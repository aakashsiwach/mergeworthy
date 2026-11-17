"""Submission: fork if needed, branch, commit as the approving human, push, open the pull request.

Every write is behind four gates, checked in ``prepare_submission`` before anything is sent:

1. the attempt is ``approved``;
2. the repository is on the allowlist and not denied or in cooldown, and the pull request limits hold;
3. the stored diff has the SHA-256 recorded at approval and touches exactly the approved files;
4. the issue is still open, unassigned and without a linked pull request (re-checked right before writing).

Dry run is the default: it prints exactly what would be sent and makes no write and runs no git. Writing needs
both ``dry_run = false`` in the configuration and ``--no-dry-run`` on the command line.

The change is never pushed from the sandbox working copy. A fresh clean clone of the upstream repository is
made on the host at the commit the attempt was based on, the approved diff text is applied there, and only the
approved files may change before the commit is made.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from mergeworthy.approve import clean, diff_sha256
from mergeworthy.config import Config
from mergeworthy.eligibility import recheck_issue
from mergeworthy.github import GitHubClient, GitHubError, _check_repo
from mergeworthy.gitops import Git, GitError, Identity
from mergeworthy.ledger import AttemptState, Ledger
from mergeworthy.patch import PatchError, require_valid
from mergeworthy.recheck import IssueReader, ensure_still_available

DEFAULT_DISCLOSURE = (
    "This change was prepared with AI assistance (an AI coding agent working in a sandbox) and then reviewed "
    "and tested by me before submission. I understand the change and can answer questions about it."
)
TITLE_LIMIT = 100


class SubmissionRefused(Exception):
    """A gate refused the submission; nothing was sent."""


# --- GitHub writes ------------------------------------------------------------------------------------------


class GitHubWriter(GitHubClient):
    """The only GitHub write paths. Every method re-checks the allowlist and dry-run itself."""

    def __init__(self, token: SecretStr, config: Config, **kwargs: Any) -> None:
        super().__init__(token, **kwargs)
        self._config = config

    def _guard(self, upstream: str) -> None:
        if self._config.dry_run:
            raise SubmissionRefused("dry_run is on in the configuration; nothing may be written to GitHub")
        if not self._config.is_allowed(upstream):
            raise SubmissionRefused(f"{upstream} is not on the allowlist")

    def _post(self, url: str, payload: dict[str, Any]) -> Any:
        response = self._http.post(url, json=payload)
        if response.status_code >= 400:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GitHubError(f"POST {url}: HTTP {response.status_code} {message}", response.status_code)
        return response.json()

    def login(self) -> str:
        return str(self._get_json("/user")["login"])

    def can_push(self, full_name: str) -> bool:
        data = self._get_json(f"/repos/{_check_repo(full_name)}")
        return bool((data.get("permissions") or {}).get("push"))

    def create_fork(self, upstream: str) -> str:
        self._guard(upstream)
        data = self._post(f"/repos/{_check_repo(upstream)}/forks", {"default_branch_only": True})
        return str(data["full_name"])

    def create_pull_request(
        self, upstream: str, *, head: str, base: str, title: str, body: str
    ) -> tuple[int, str]:
        self._guard(upstream)
        data = self._post(
            f"/repos/{_check_repo(upstream)}/pulls",
            {"title": title, "head": head, "base": base, "body": body, "maintainer_can_modify": True},
        )
        return int(data["number"]), str(data["html_url"])


# --- planning (no writes) -------------------------------------------------------------------------


@dataclass(frozen=True)
class SubmissionPlan:
    attempt_id: int
    upstream: str
    issue_number: int
    issue_url: str
    base_branch: str
    base_commit: str
    branch: str
    files: list[str]
    diff: str
    diff_sha256: str
    title: str
    body: str
    commit_message: str
    identity: Identity
    warnings: list[str] = field(default_factory=list)


def _clean_title(text: str) -> str:
    title = " ".join(clean(text).split())
    return title if len(title) <= TITLE_LIMIT else title[: TITLE_LIMIT - 3] + "..."


def pull_request_body(
    *, issue_number: int, explanation: str, verification: list[dict[str, Any]], disclosure: str
) -> str:
    tested = []
    labels = {
        "baseline": "The test suite passed on the unmodified repository.",
        "tests_pass": "The test suite passes with the change.",
        "tests_fail_without_fix": "The new or changed tests fail without the fix and pass with it.",
        "linters": "The project's configured linters and formatters pass on the changed files.",
        "docs_build": "The documentation build passes.",
    }
    for rule in verification:
        if rule.get("passed") and rule.get("rule") in labels:
            line = labels[rule["rule"]]
            if rule["rule"] == "tests_fail_without_fix" and "exempt" in str(rule.get("reason", "")):
                line = "Documentation-only change, so no new test."
            if rule["rule"] in ("linters", "docs_build") and str(rule.get("reason", "")).startswith("no "):
                continue
            tested.append(f"- {line}")
    summary = clean(explanation).strip() or "(no summary)"
    return (
        f"Fixes #{issue_number}\n\n"
        f"## Summary\n\n{summary}\n\n"
        "## How it was tested\n\n"
        + ("\n".join(tested) or "- (no automated checks recorded)")
        + "\n\nAll checks ran locally in an isolated container.\n\n"
        f"## AI assistance\n\n{disclosure.strip()}\n"
    )


def _policy_disclosure(ledger: Ledger, upstream: str) -> list[str]:
    record = ledger.get_repository(upstream) or {}
    ai_policy = (record.get("policy_check") or {}).get("ai_policy") or {}
    return list((ai_policy.get("details") or {}).get("disclosure") or [])


def prepare_submission(
    gh: IssueReader,
    ledger: Ledger,
    config: Config,
    attempt_id: int,
    *,
    identity: Identity,
    now: datetime,
    disclosure: str | None = None,
) -> SubmissionPlan:
    """Check every gate and build the exact submission. Raises ``SubmissionRefused``; writes nothing."""
    details = ledger.attempt_details(attempt_id)
    if details["state"] != AttemptState.APPROVED.value:
        raise SubmissionRefused(
            f"attempt {attempt_id} is {details['state']}; only approved attempts are submitted"
        )
    issue = ledger.get_issue(details["issue_id"]) or {}
    parts = str(issue.get("url", "")).rstrip("/").split("/")
    if len(parts) < 4 or parts[-2] != "issues":
        raise SubmissionRefused(f"attempt {attempt_id} has no issue URL")
    upstream, number = f"{parts[-4]}/{parts[-3]}", int(parts[-1])

    # Gate 2: where and how often.
    if not config.is_allowed(upstream):
        raise SubmissionRefused(f"{upstream} is not on the allowlist in mergeworthy.toml")
    repo_record = ledger.get_repository(upstream) or {}
    if repo_record.get("deny_listed"):
        raise SubmissionRefused(f"{upstream} is on the deny list")
    cooldown = repo_record.get("cooldown_until")
    if cooldown and datetime.fromisoformat(cooldown) > now:
        raise SubmissionRefused(f"{upstream} is in cooldown until {cooldown}")
    week = ledger.pull_requests_since((now - timedelta(days=7)).isoformat(timespec="seconds"))
    if week >= config.limits.prs_per_week:
        raise SubmissionRefused(f"weekly limit reached: {week} pull requests in the last 7 days")
    open_here = len(ledger.pull_requests(state="open", repository=upstream))
    if open_here >= config.limits.open_prs_per_repository:
        raise SubmissionRefused(f"{upstream} already has {open_here} open pull request(s) from mergeworthy")

    # Gate 3: exactly what was approved.
    diff = details.get("diff") or ""
    approved_sha = details.get("approved_diff_sha256")
    if not approved_sha or diff_sha256(diff) != approved_sha:
        raise SubmissionRefused("the stored diff does not match the SHA-256 recorded at approval")
    approved_files = details.get("approved_files") or []
    try:
        report = require_valid(diff, approved_files)
    except PatchError as exc:
        raise SubmissionRefused(f"the diff does not match the approved files: {exc}") from exc
    base_commit = details.get("base_commit")
    if not base_commit:
        raise SubmissionRefused("the attempt has no recorded base commit")

    # Gate 4 (read-only here; ``submit`` re-checks and may abandon right before writing).
    live = gh.get_issue(upstream, number)
    recheck = recheck_issue(gh, config, live)
    if not recheck.eligible:
        raise SubmissionRefused(f"the issue is no longer available: {recheck.summary()}")

    warnings: list[str] = []
    project_wording = _policy_disclosure(ledger, upstream)
    if project_wording and disclosure is None:
        warnings.append(
            "the project asks for specific AI disclosure wording; pass --disclosure with it: "
            + " | ".join(clean(q) for q in project_wording)
        )
    repo = gh.get_repository(upstream)
    title = _clean_title(str(issue.get("title", "")))
    body = pull_request_body(
        issue_number=number,
        explanation=details.get("explanation") or "",
        verification=details.get("verification") or [],
        disclosure=disclosure or DEFAULT_DISCLOSURE,
    )
    return SubmissionPlan(
        attempt_id=attempt_id,
        upstream=upstream,
        issue_number=number,
        issue_url=str(issue.get("url")),
        base_branch=repo.default_branch,
        base_commit=base_commit,
        branch=details.get("branch") or f"mergeworthy/issue-{number}-{attempt_id}",
        files=report.files,
        diff=diff,
        diff_sha256=approved_sha,
        title=title,
        body=body,
        commit_message=f"{title}\n\nFixes #{number}\n",
        identity=identity,
        warnings=warnings,
    )


def render_plan(plan: SubmissionPlan, git: Git, *, push_repository: str, fork_needed: bool) -> str:
    """Exactly what a real submission sends."""
    payload = {
        "title": plan.title,
        "head": f"{push_repository.split('/')[0]}:{plan.branch}",
        "base": plan.base_branch,
        "body": plan.body,
        "maintainer_can_modify": True,
    }
    lines = [f"DRY RUN: attempt {plan.attempt_id} -> {plan.upstream}#{plan.issue_number}", ""]
    if fork_needed:
        lines.append(f"1. POST /repos/{plan.upstream}/forks {json.dumps({'default_branch_only': True})}")
    else:
        lines.append(f"1. no fork: you can push to {plan.upstream}")
    lines += [
        f"2. fresh clone of {git.url_for(plan.upstream)} at {plan.base_commit}, new branch {plan.branch}",
        f"3. apply the approved diff (sha256 {plan.diff_sha256}); only these files may change:",
        *[f"     {path}" for path in plan.files],
        f"4. commit as {plan.identity.name} <{plan.identity.email}> with message:",
        *[f"     {line}" for line in plan.commit_message.rstrip().splitlines()],
        f"5. push to {git.url_for(push_repository)} refs/heads/{plan.branch}",
        f"6. POST /repos/{plan.upstream}/pulls",
        json.dumps(payload, indent=2),
    ]
    if plan.warnings:
        lines += ["", *[f"WARNING: {w}" for w in plan.warnings]]
    return "\n".join(lines)


# --- sending --------------------------------------------------------------------------------------


def remove_tree(path: Path) -> None:
    """Delete a directory, including git's read-only object files on Windows."""

    def make_writable_and_retry(func: Any, target: str, _exc: Any) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable_and_retry)
    else:
        shutil.rmtree(path, onerror=make_writable_and_retry)


@dataclass(frozen=True)
class SubmissionResult:
    number: int
    url: str
    commit: str


def submit(
    plan: SubmissionPlan,
    *,
    gh: IssueReader,
    writer: GitHubWriter,
    ledger: Ledger,
    config: Config,
    git: Git,
    token: SecretStr,
    workspace: Path | None = None,
) -> SubmissionResult:
    """Send the planned submission. Call only after ``prepare_submission`` and outside dry run."""
    if plan.warnings:
        raise SubmissionRefused("; ".join(plan.warnings))
    if config.dry_run:
        raise SubmissionRefused("dry_run is on in the configuration")
    # Re-check immediately before forking or pushing; abandons the attempt if the issue was taken.
    ensure_still_available(gh, ledger, config, plan.attempt_id)

    login = writer.login()
    push_repository = plan.upstream if writer.can_push(plan.upstream) else writer.create_fork(plan.upstream)
    if not push_repository.lower().startswith(f"{login.lower()}/") and push_repository != plan.upstream:
        raise SubmissionRefused(f"fork {push_repository} does not belong to {login}")

    root = Path(tempfile.mkdtemp(prefix="mergeworthy-submit-", dir=workspace))
    clone = root / "repo"
    try:
        git.fresh_clone(plan.upstream, plan.base_commit, plan.branch, clone)
        git.apply(plan.diff, clone)
        changed = git.changed_files(clone)
        if changed != sorted(plan.files):
            raise SubmissionRefused(f"applying the diff changed {changed}, approved {sorted(plan.files)}")
        commit = git.commit(clone, plan.files, plan.identity, plan.commit_message)
        git.push(clone, push_repository, plan.branch, token.get_secret_value())
    except GitError as exc:
        raise SubmissionRefused(f"git: {exc}") from exc
    finally:
        remove_tree(root)

    head = f"{push_repository.split('/')[0]}:{plan.branch}"
    number, url = writer.create_pull_request(
        plan.upstream, head=head, base=plan.base_branch, title=plan.title, body=plan.body
    )
    ledger.record_submission(
        plan.attempt_id,
        repository=plan.upstream,
        number=number,
        url=url,
        head_repository=push_repository,
        branch=plan.branch,
        reason=f"pull request {url} opened from {push_repository} at {commit}",
    )
    return SubmissionResult(number, url, commit)
