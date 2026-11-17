"""Human approval in the terminal (docs/policy.md section 5).

Shows everything a person needs to decide: the issue, the agent's summary, the full diff, test results,
verification, self-review, policy checks and spend. Approval and rejection both need a reason. Approval pins a
SHA-256 of the exact diff that was shown and the list of files, so submission can only send what was approved.

All text from the issue, the repository and the models is untrusted: control characters (including terminal
escape sequences) are removed before it is shown.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from mergeworthy.ledger import AttemptState, Ledger, LedgerError
from mergeworthy.patch import validate_patch

_CONTROL = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)?|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def clean(text: str | None) -> str:
    """Untrusted text, safe to print: no control characters or escape sequences (newlines and tabs kept)."""
    return _CONTROL.sub("", text or "")


def diff_sha256(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Waiting:
    attempt_id: int
    issue_ref: str
    title: str
    files: int
    lines: int
    spend_usd: float
    verdict: str | None


def _issue_ref(url: str) -> str:
    parts = url.rstrip("/").split("/")
    return f"{parts[-4]}/{parts[-3]}#{parts[-1]}" if len(parts) >= 4 else url


def waiting_attempts(ledger: Ledger) -> list[Waiting]:
    items = []
    for attempt in ledger.attempts_in_state(AttemptState.AWAITING_APPROVAL):
        details = ledger.attempt_details(attempt.id)
        issue = ledger.get_issue(attempt.issue_id) or {}
        stats = details.get("diff_stats") or {}
        items.append(
            Waiting(
                attempt_id=attempt.id,
                issue_ref=_issue_ref(issue.get("url", "")),
                title=clean(issue.get("title", "")),
                files=len(stats.get("files", [])),
                lines=int(stats.get("lines_changed_excluding_tests", 0)),
                spend_usd=float(details.get("spend_usd") or 0.0),
                verdict=details.get("review_verdict"),
            )
        )
    return items


def _section(title: str) -> list[str]:
    return ["", f"== {title} " + "=" * max(0, 70 - len(title))]


def render_summary(ledger: Ledger, attempt_id: int) -> str:
    """Everything except the diff, as plain text."""
    details = ledger.attempt_details(attempt_id)
    issue = ledger.get_issue(details["issue_id"]) or {}
    lines = [f"Attempt {attempt_id}: {_issue_ref(issue.get('url', ''))}  [{details['state']}]"]

    lines += _section("Issue")
    lines.append(f"Title:  {clean(issue.get('title'))}")
    lines.append(f"URL:    {clean(issue.get('url'))}")
    lines.append(f"Labels: {', '.join(clean(label) for label in issue.get('labels', []))}")

    lines += _section("Summary from the coding agent (unverified claims)")
    lines.append(clean(details.get("explanation")) or "(none)")

    stats = details.get("diff_stats") or {}
    lines += _section("Change")
    lines.append(
        f"{len(stats.get('files', []))} files, {stats.get('lines_changed_excluding_tests', 0)} lines changed "
        f"excluding tests, {stats.get('test_lines_changed', 0)} test lines"
    )
    for path in stats.get("files", []):
        marker = " (test)" if path in stats.get("test_files", []) else ""
        lines.append(f"  {clean(path)}{marker}")

    lines += _section("Tests")
    baseline = (details.get("test_results") or {}).get("baseline") or {}
    lines.append(
        f"baseline: exit {baseline.get('exit_code')}, {baseline.get('seconds', 0):.0f}s"
        + (" (timed out)" if baseline.get("timed_out") else "")
    )
    for command in details.get("commands") or []:
        lines.append(f"agent ran: {clean(command.get('command'))} -> exit {command.get('exit_code')}")

    lines += _section("Verification")
    for rule in details.get("verification") or []:
        if (rule.get("details") or {}).get("informational"):
            mark = "NOTE"
        else:
            mark = "PASS" if rule.get("passed") else "FAIL"
        lines.append(f"[{mark}] {rule.get('rule')}: {clean(rule.get('reason'))}")

    review: dict[str, Any] = details.get("review_reasons") or {}
    lines += _section(
        f"Self-review: {details.get('review_verdict')} (rubric v{review.get('rubric_version')})"
    )
    for question, answer in (review.get("answers") or {}).items():
        lines.append(f"[{answer.get('answer', '?'):>3}] {question}: {clean(answer.get('reason'))}")
    for reason in review.get("reasons") or []:
        if not review.get("answers"):
            lines.append(clean(reason))

    lines += _section("Policy checks")
    for check in issue.get("eligibility") or []:
        lines.append(f"[{check.get('status', '?'):>8}] {check.get('name')}: {clean(check.get('reason'))}")

    lines += _section("Spend")
    lines.append(f"${float(details.get('spend_usd') or 0.0):.2f} (agent and self-review)")
    return "\n".join(lines)


def render_diff(ledger: Ledger, attempt_id: int) -> str:
    return clean(ledger.attempt_details(attempt_id).get("diff"))


class ApprovalError(Exception):
    pass


def approve(ledger: Ledger, attempt_id: int, reason: str, shown_diff: str) -> str:
    """Approve the attempt. ``shown_diff`` is the diff the person saw; it must match the stored diff."""
    if not reason.strip():
        raise ApprovalError("a reason is required to approve")
    details = ledger.attempt_details(attempt_id)
    if details["state"] != AttemptState.AWAITING_APPROVAL.value:
        raise ApprovalError(f"attempt {attempt_id} is {details['state']}, not awaiting approval")
    diff = details.get("diff") or ""
    if not diff.strip():
        raise ApprovalError(f"attempt {attempt_id} has no diff")
    if clean(diff) != shown_diff:
        raise ApprovalError("the diff changed since it was shown; review it again")
    report = validate_patch(diff)
    if not report.ok:
        raise ApprovalError(f"the diff is not a safe patch: {'; '.join(report.problems)}")
    digest = diff_sha256(diff)
    try:
        ledger.record_approval(attempt_id, digest, report.files, f"approved by a human: {reason.strip()}")
    except LedgerError as exc:
        raise ApprovalError(str(exc)) from exc
    return digest


def reject(ledger: Ledger, attempt_id: int, reason: str) -> None:
    if not reason.strip():
        raise ApprovalError("a reason is required to reject")
    state = ledger.get_attempt(attempt_id).state
    if state is not AttemptState.AWAITING_APPROVAL:
        raise ApprovalError(f"attempt {attempt_id} is {state}, not awaiting approval")
    ledger.transition(attempt_id, AttemptState.HUMAN_REJECTED, f"rejected by a human: {reason.strip()}")
