"""Re-check an issue right before acting on it.

The coding agent (T5) and submission (T8) must call ``ensure_still_available`` immediately before they start.
If the issue was closed, assigned to someone else, or a pull request now links to it, the attempt moves to
``abandoned`` and ``NoLongerAvailable`` is raised. If a pull request only mentions the issue, a human decides
(``mergeworthy decide owner/repo#N no_open_pull_request``) and nothing proceeds until then.
"""

from __future__ import annotations

from typing import Protocol

from mergeworthy.config import Config
from mergeworthy.eligibility import GitHubReader, Report, apply_decisions, recheck_issue
from mergeworthy.github import Issue
from mergeworthy.ledger import TERMINAL_STATES, AttemptState, Ledger


class IssueReader(GitHubReader, Protocol):
    def get_issue(self, full_name: str, number: int) -> Issue: ...


class NotAvailable(Exception):
    def __init__(self, attempt_id: int, report: Report, message: str) -> None:
        super().__init__(message)
        self.attempt_id = attempt_id
        self.report = report


class NoLongerAvailable(NotAvailable):
    """The issue was taken or closed; the attempt was abandoned."""


class AwaitingHumanDecision(NotAvailable):
    """A check is unclear; the attempt is left unchanged until a human decides."""


def ensure_still_available(gh: IssueReader, ledger: Ledger, config: Config, attempt_id: int) -> Report:
    attempt = ledger.get_attempt(attempt_id)
    if attempt.state in TERMINAL_STATES:
        raise NoLongerAvailable(
            attempt_id, Report("", []), f"attempt {attempt_id} is already {attempt.state}"
        )
    issue_record = ledger.get_issue(attempt.issue_id)
    if issue_record is None:
        raise NoLongerAvailable(attempt_id, Report("", []), f"attempt {attempt_id} has no issue")
    full_name, number = _issue_ref(issue_record["url"])
    issue = gh.get_issue(full_name, number)

    def lookup(subject: str, check_name: str, digest: str) -> tuple[str, str] | None:
        found = ledger.find_decision(subject, check_name, digest)
        if found is None or found.decision is None:
            return None
        return found.decision.value, found.reason or ""

    report = apply_decisions(recheck_issue(gh, config, issue), lookup)
    if report.blocked:
        reasons = "; ".join(f"{r.name}: {r.reason}" for r in report.failures)
        ledger.transition(attempt_id, AttemptState.ABANDONED, f"re-check before acting: {reasons}")
        raise NoLongerAvailable(attempt_id, report, f"attempt {attempt_id} abandoned: {reasons}")
    if not report.eligible:
        for result in report.results:
            digest = result.details.get("content_hash")
            if isinstance(digest, str) and not result.passed:
                ledger.request_decision(report.subject, result.name, digest, result.reason)
        reasons = "; ".join(f"{r.name}: {r.reason}" for r in report.failures)
        raise AwaitingHumanDecision(attempt_id, report, f"attempt {attempt_id} waits for a human: {reasons}")
    return report


def _issue_ref(url: str) -> tuple[str, int]:
    # https://github.com/owner/repo/issues/12
    parts = url.rstrip("/").split("/")
    return f"{parts[-4]}/{parts[-3]}", int(parts[-1])
