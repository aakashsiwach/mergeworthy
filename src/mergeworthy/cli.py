"""Command-line interface."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from mergeworthy import __version__
from mergeworthy.agent import CodingAgent
from mergeworthy.approve import ApprovalError, approve, reject, render_diff, render_summary, waiting_attempts
from mergeworthy.config import Config, ConfigError, load_config, load_secrets
from mergeworthy.discover import (
    CheckOutcome,
    DiscoveryReader,
    check_and_record,
    discover,
    record_decision,
)
from mergeworthy.eligibility import Report, Status
from mergeworthy.github import GitHubClient, GitHubError
from mergeworthy.ledger import AttemptState, Decision, Ledger, LedgerError
from mergeworthy.recheck import AwaitingHumanDecision, NoLongerAvailable
from mergeworthy.review import ClaudeReviewer, Reviewer
from mergeworthy.sandbox import SandboxError, docker_available
from mergeworthy.work import STARTABLE, SandboxFactory, default_sandbox, plan_work, run_attempt

# `mergeworthy work` exit codes (docs/architecture.md).
EXIT_REJECTED = 3
EXIT_NEEDS_HUMAN = 4

app = typer.Typer(
    name="mergeworthy",
    help="An autonomous open-source contributor that only submits work a human has approved.",
    no_args_is_help=True,
    add_completion=False,
)

console = Console(highlight=False)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Configuration file (default: ./mergeworthy.toml)."),
]

# Control characters, including ANSI escape sequences, are stripped from untrusted text before display.
_CONTROL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def safe(text: str) -> Text:
    """Untrusted text (issue titles, repository content) as plain, non-markup terminal text."""
    return Text(_CONTROL.sub("", text))


def make_client() -> DiscoveryReader:
    """A read-only GitHub client. The token is optional for reading but raises rate limits."""
    return GitHubClient(load_secrets().github_token)


@contextmanager
def _client() -> Iterator[DiscoveryReader]:
    gh = make_client()
    try:
        yield gh
    finally:
        close = getattr(gh, "close", None)
        if callable(close):
            close()


def _load(config_path: Path | None) -> Config:
    try:
        return load_config(config_path)
    except ConfigError as exc:
        console.print(safe(f"configuration error: {exc}"), style="red")
        raise typer.Exit(2) from exc


@app.callback()
def main() -> None:
    """mergeworthy command-line interface."""


@app.command()
def version() -> None:
    """Print the installed version."""
    typer.echo(f"mergeworthy {__version__}")


@app.command(name="discover")
def discover_command(
    language: Annotated[
        list[str] | None, typer.Option("--language", "-l", help="java or python; repeatable.")
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", "-n", min=1, max=1000, help="Candidates per language.")
    ] = 30,
    config_path: ConfigOption = None,
) -> None:
    """Find candidate issues and record them in the ledger."""
    config = _load(config_path)
    languages = [lang.lower() for lang in language] if language else None
    try:
        with Ledger(config.ledger_path) as ledger, _client() as gh:
            found = discover(gh, ledger, config, languages=languages, limit=limit)
    except (ValueError, GitHubError) as exc:
        console.print(safe(f"error: {exc}"), style="red")
        raise typer.Exit(1) from exc

    table = Table("issue", "language", "title", "status")
    for item in found:
        ref = f"{item.issue.repository}#{item.issue.number}"
        table.add_row(safe(ref), item.language, safe(item.issue.title), "new" if item.new else "known")
    console.print(table)
    new = sum(item.new for item in found)
    console.print(f"{len(found)} candidates, {new} new. Run `mergeworthy check OWNER/REPO#N` next.")


_STYLE = {Status.PASS: "green", Status.FAIL: "red", Status.UNCLEAR: "yellow", Status.DEFERRED: "cyan"}


def _print_report(report: Report) -> None:
    table = Table("check", "result", "reason", title=safe(report.subject))
    for result in report.results:
        table.add_row(
            result.name, Text(result.status.value, style=_STYLE[result.status]), safe(result.reason)
        )
        for match in result.details.get("matches", [])[:3]:
            table.add_row("", "", safe(f'  {match["source"]}: "{match["quote"]}"'))
    console.print(table)


@app.command(name="check")
def check_command(
    target: Annotated[str, typer.Argument(help="owner/repo, owner/repo#N, or a GitHub URL.")],
    config_path: ConfigOption = None,
) -> None:
    """Run the eligibility checks of docs/policy.md and print the reasons.

    Exits 0 when eligible, 1 when not eligible, 3 when a human must decide.
    """
    config = _load(config_path)
    try:
        with Ledger(config.ledger_path) as ledger, _client() as gh:
            outcome: CheckOutcome = check_and_record(gh, ledger, config, target, now=datetime.now(UTC))
    except (ValueError, GitHubError) as exc:
        console.print(safe(f"error: {exc}"), style="red")
        raise typer.Exit(1) from exc

    _print_report(outcome.repository)
    if outcome.issue is not None:
        _print_report(outcome.issue)
    if outcome.attempt_id is not None and outcome.attempt_state is not None:
        console.print(f"attempt {outcome.attempt_id}: {outcome.attempt_state.value}")
    if outcome.eligible:
        console.print("eligible", style="green")
        return
    if outcome.needs_human:
        console.print("skipped: needs a human decision", style="yellow")
        raise typer.Exit(3)
    console.print("not eligible", style="red")
    raise typer.Exit(1)


@app.command(name="decide")
def decide_command(
    subject: Annotated[
        str | None, typer.Argument(help="owner/repo or owner/repo#N; omit to list open questions.")
    ] = None,
    check: Annotated[str | None, typer.Argument(help="The check to decide, e.g. ai_policy.")] = None,
    allow: Annotated[bool, typer.Option("--allow", help="Allow: treat the check as passed.")] = False,
    deny: Annotated[bool, typer.Option("--deny", help="Deny: treat the check as failed.")] = False,
    reason: Annotated[str, typer.Option("--reason", "-r", help="Why; required.")] = "",
    config_path: ConfigOption = None,
) -> None:
    """List or answer questions that need a human decision.

    A decision applies only to the content it was based on (for example the repository's policy files). If
    that content changes, the question is asked again.
    """
    config = _load(config_path)
    with Ledger(config.ledger_path) as ledger:
        if subject is None:
            pending = ledger.pending_decisions()
            table = Table("subject", "check", "question", "asked")
            for item in pending:
                table.add_row(safe(item.subject), item.check_name, safe(item.question), item.requested_at)
            console.print(table)
            console.print(f"{len(pending)} open questions.")
            return
        if check is None or allow == deny or not reason.strip():
            console.print("give SUBJECT CHECK, exactly one of --allow or --deny, and --reason", style="red")
            raise typer.Exit(2)
        decision = Decision.ALLOW if allow else Decision.DENY
        try:
            outcome = record_decision(ledger, subject, check, decision, reason)
        except (ValueError, LedgerError) as exc:
            console.print(safe(f"error: {exc}"), style="red")
            raise typer.Exit(1) from exc
    console.print(safe(f"recorded: {decision.value} {outcome.record.subject} {check}"))
    if outcome.skipped_attempts:
        console.print(f"skipped attempts: {', '.join(map(str, outcome.skipped_attempts))}")
    if outcome.abandoned_attempts:
        console.print(f"abandoned attempts: {', '.join(map(str, outcome.abandoned_attempts))}")
    if outcome.resumable_attempts:
        ids = ", ".join(map(str, outcome.resumable_attempts))
        console.print(f"resumable attempts: {ids} (run `mergeworthy work` again to continue)")
    if decision is Decision.ALLOW:
        console.print(safe(f"run `mergeworthy check {outcome.record.subject}` to re-evaluate."))


def make_agent(config: Config) -> CodingAgent:
    """The real coding agent. Imported lazily so read-only commands never load the SDK."""
    from mergeworthy.agent.claude import ClaudeCodingAgent

    return ClaudeCodingAgent(model=config.agent.model)


def make_reviewer(config: Config) -> Reviewer:
    return ClaudeReviewer(model=config.review.model, prices=config.prices)


def make_sandbox_factory() -> SandboxFactory:
    return default_sandbox


def sandbox_ready() -> bool:
    return docker_available()


@app.command(name="work")
def work_command(
    issue: Annotated[str, typer.Argument(help="Issue URL or owner/repo#N.")],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run/--no-dry-run", help="Dry run (default): show the plan; start nothing, spend nothing."
        ),
    ] = True,
    config_path: ConfigOption = None,
) -> None:
    """Run one attempt end to end up to verification. Dry run by default.

    Exit codes: 0 the attempt is awaiting approval (or the dry run found the issue workable), 3 rejected by
    self-review, 4 a human must decide something first, 1 any other error or failure.
    """
    config = _load(config_path)
    now = datetime.now(UTC)
    try:
        with Ledger(config.ledger_path) as ledger, _client() as gh:
            plan = plan_work(gh, ledger, config, issue, now=now)
            _print_report(plan.repository)
            _print_report(plan.issue)
            if plan.claim_required:
                console.print(
                    "the contributing guide asks to claim issues first; claiming is not built yet",
                    style="red",
                )
                raise typer.Exit(1)
            if plan.blocked:
                console.print("not eligible", style="red")
                raise typer.Exit(1)
            if not plan.eligible:
                console.print("needs a human decision first: `mergeworthy decide`", style="yellow")
                raise typer.Exit(EXIT_NEEDS_HUMAN)
            if dry_run:
                console.print(safe(f"dry run: {plan.target}"), style="bold")
                for number, step in enumerate(plan.steps(config), 1):
                    console.print(safe(f"  {number}. {step}"))
                console.print("nothing was started. Run with --no-dry-run to do it.")
                return
            if not sandbox_ready():
                console.print("Docker is not available; start it and try again", style="red")
                raise typer.Exit(1)
            checked = check_and_record(gh, ledger, config, plan.target, now=now)
            if checked.attempt_id is None or checked.attempt_state not in STARTABLE:
                state = checked.attempt_state.value if checked.attempt_state else "none"
                console.print(f"attempt is {state}; work starts only from eligible or claimed", style="red")
                raise typer.Exit(1)
            outcome = run_attempt(
                gh,
                ledger,
                config,
                checked.attempt_id,
                make_agent(config),
                reviewer=make_reviewer(config),
                sandbox_factory=make_sandbox_factory(),
            )
    except AwaitingHumanDecision as exc:
        console.print(safe(f"paused: {exc}"), style="yellow")
        raise typer.Exit(EXIT_NEEDS_HUMAN) from exc
    except NoLongerAvailable as exc:
        console.print(safe(f"abandoned: {exc}"), style="red")
        raise typer.Exit(1) from exc
    except (ValueError, GitHubError, SandboxError) as exc:
        console.print(safe(f"error: {exc}"), style="red")
        raise typer.Exit(1) from exc

    console.print(safe(f"attempt {outcome.attempt_id}: {outcome.state.value} ({outcome.reason})"))
    if outcome.result is not None:
        console.print(f"spend: ${outcome.result.spend_usd:.2f}")
    if outcome.state is AttemptState.REJECTED:
        raise typer.Exit(EXIT_REJECTED)
    if outcome.state is not AttemptState.AWAITING_APPROVAL:
        raise typer.Exit(1)


def _show(text: str, pager: bool) -> None:
    if pager:
        click.echo_via_pager(text)
    else:
        click.echo(text)


@app.command(name="review")
def review_command(
    attempt: Annotated[
        int | None, typer.Argument(help="Attempt to review; omit to list waiting attempts.")
    ] = None,
    pager: Annotated[bool, typer.Option("--pager/--no-pager", help="Page the diff.")] = True,
    config_path: ConfigOption = None,
) -> None:
    """Approve or reject attempts waiting for a human. Every decision needs a reason."""
    config = _load(config_path)
    with Ledger(config.ledger_path) as ledger:
        waiting = waiting_attempts(ledger)
        if attempt is None:
            if not waiting:
                console.print("no attempts are waiting for approval")
                return
            table = Table("attempt", "issue", "title", "files", "lines", "spend", "self-review")
            for item in waiting:
                table.add_row(
                    str(item.attempt_id),
                    safe(item.issue_ref),
                    safe(item.title),
                    str(item.files),
                    str(item.lines),
                    f"${item.spend_usd:.2f}",
                    item.verdict or "",
                )
            console.print(table)
            choice = typer.prompt("Attempt to review (empty to quit)", default="", show_default=False).strip()
            if not choice:
                return
            if not choice.isdigit():
                console.print("not an attempt number", style="red")
                raise typer.Exit(2)
            attempt = int(choice)
        if attempt not in {item.attempt_id for item in waiting}:
            console.print(f"attempt {attempt} is not waiting for approval", style="red")
            raise typer.Exit(1)

        click.echo(render_summary(ledger, attempt))
        shown_diff = render_diff(ledger, attempt)
        click.echo("\n== Diff " + "=" * 66)
        _show(shown_diff, pager)

        action = typer.prompt("Approve, reject or skip? [a/r/s]", default="s").strip().lower()
        if action in ("s", "skip"):
            console.print("skipped; nothing recorded")
            return
        if action not in ("a", "approve", "r", "reject"):
            console.print("answer a, r or s; nothing recorded", style="red")
            raise typer.Exit(2)
        reason = typer.prompt("Reason (required)", default="", show_default=False).strip()
        if not reason:
            console.print("a reason is required; nothing recorded", style="red")
            raise typer.Exit(1)
        try:
            if action in ("a", "approve"):
                digest = approve(ledger, attempt, reason, shown_diff)
                console.print(f"attempt {attempt} approved (diff sha256 {digest[:12]})", style="green")
                console.print("submission (T8) will send exactly this diff; it is dry-run by default")
            else:
                reject(ledger, attempt, reason)
                console.print(f"attempt {attempt} rejected", style="yellow")
        except ApprovalError as exc:
            console.print(safe(f"error: {exc}"), style="red")
            raise typer.Exit(1) from exc
