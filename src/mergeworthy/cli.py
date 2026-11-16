"""Command-line interface."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from mergeworthy import __version__
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
from mergeworthy.ledger import Decision, Ledger, LedgerError

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
    if decision is Decision.ALLOW:
        console.print(safe(f"run `mergeworthy check {outcome.record.subject}` to re-evaluate."))
