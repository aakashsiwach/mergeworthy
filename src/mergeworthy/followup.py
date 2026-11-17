"""Follow-up on submitted pull requests (docs/policy.md section 6).

Read-only on GitHub: polls each open pull request for its state, CI checks, comments and reviews, updates
the ledger, and stores drafts (replies, fix attempts, closing) that need a human's approval before anything
is sent.
It never posts, so it never pings maintainers.

- A pull request seen open moves its attempt from ``submitted`` to ``in_review``.
- Merged: ``merged``. Closed without merge: ``closed``, the repository enters the cooldown, and the last
  maintainer comment is kept as the outcome note.
- A maintainer saying AI contributions are unwelcome puts the repository on the deny list at once, and a
  closing-with-thanks draft is stored for approval. A maintainer asking to close the pull request gets the
  same closing draft.
- A new maintainer comment or review gets a reply draft; a CI failure on the current head gets a fix draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from mergeworthy.ai_policy import PolicyVerdict, classify
from mergeworthy.config import Config
from mergeworthy.github import CheckRun, Comment, PullRequestStatus, Review
from mergeworthy.ledger import AttemptState, Ledger

MAINTAINERS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
FAILED = frozenset({"failure", "timed_out", "cancelled", "action_required", "startup_failure"})
PASSED = frozenset({"success", "neutral", "skipped"})

# Said by a maintainer about this pull request; in addition to the AI policy phrase list.
_UNWELCOME = re.compile(
    r"\b(?:ai|a\.i\.|llm|chatgpt|copilot|bot|generated)\b[^.!?\n]{0,60}"
    r"\b(?:not welcome|unwelcome|not accepted|don'?t accept|do not accept|spam|slop|banned)\b"
    r"|\b(?:ai|llm)[- ]?slop\b|\bslop\b",
    re.IGNORECASE,
)
_CLOSE_REQUEST = re.compile(
    r"\b(?:please\s+)?close\s+(?:this|the)\s+(?:pr|pull request)\b|\bclosing\s+(?:this|in favou?r of)\b"
    r"|\bwon'?t\s+(?:be\s+)?merg(?:e|ed|ing)\s+this\b",
    re.IGNORECASE,
)


class FollowupReader(Protocol):
    def get_pull_request(self, full_name: str, number: int) -> PullRequestStatus: ...
    def list_check_runs(self, full_name: str, ref: str) -> list[CheckRun]: ...
    def list_issue_comments(self, full_name: str, number: int, *, limit: int = 300) -> list[Comment]: ...
    def list_reviews(self, full_name: str, number: int) -> list[Review]: ...


@dataclass(frozen=True)
class Feedback:
    source: str
    author: str
    body: str
    created_at: str


@dataclass
class FollowupResult:
    attempt_id: int
    url: str
    state: str
    ci_status: str
    events: list[str] = field(default_factory=list)


def ci_status(runs: list[CheckRun]) -> str:
    if not runs:
        return "none"
    if any(r.conclusion in FAILED for r in runs):
        return "failure"
    if any(r.status != "completed" for r in runs):
        return "pending"
    if all(r.conclusion in PASSED for r in runs):
        return "success"
    return "unknown"


def says_ai_unwelcome(text: str) -> bool:
    return bool(_UNWELCOME.search(text)) or classify({"comment": text}).verdict is PolicyVerdict.FORBIDS


def asks_to_close(text: str) -> bool:
    return bool(_CLOSE_REQUEST.search(text))


def _maintainer_feedback(
    comments: list[Comment], reviews: list[Review], own_login: str | None
) -> list[Feedback]:
    found: list[Feedback] = []
    for c in comments:
        if c.author_association in MAINTAINERS and c.author != own_login and c.body.strip():
            found.append(Feedback(f"comment:{c.id}", c.author or "", c.body, c.created_at.isoformat()))
    for r in reviews:
        if r.author_association in MAINTAINERS and r.author != own_login and (r.body.strip() or r.state):
            when = r.submitted_at.isoformat() if r.submitted_at else ""
            found.append(Feedback(f"review:{r.id}", r.author or "", f"[{r.state}] {r.body}".strip(), when))
    return sorted(found, key=lambda f: f.created_at)


def _move(ledger: Ledger, attempt_id: int, target: AttemptState, reason: str) -> None:
    state = ledger.get_attempt(attempt_id).state
    if state is AttemptState.SUBMITTED and target in (AttemptState.MERGED,):
        ledger.transition(attempt_id, AttemptState.IN_REVIEW, "pull request seen open")
        state = AttemptState.IN_REVIEW
    if state is not target:
        ledger.transition(attempt_id, target, reason)


def follow_up(gh: FollowupReader, ledger: Ledger, config: Config, *, now: datetime) -> list[FollowupResult]:
    results: list[FollowupResult] = []
    for pr in ledger.pull_requests(state="open"):
        attempt_id = int(pr["attempt_id"])
        repo, number = str(pr["repository"]), int(pr["number"])
        status = gh.get_pull_request(repo, number)
        runs = gh.list_check_runs(repo, status.head_sha)
        ci = ci_status(runs)
        feedback = _maintainer_feedback(
            gh.list_issue_comments(repo, number), gh.list_reviews(repo, number), config.github_login
        )
        result = FollowupResult(attempt_id, status.html_url, status.state, ci)
        state = ledger.get_attempt(attempt_id).state

        # AI unwelcome: deny list now, whatever else happens.
        unwelcome = [f for f in feedback if says_ai_unwelcome(f.body)]
        if unwelcome:
            ledger.upsert_repository(repo, deny_listed=True)
            result.events.append(
                f"{repo} added to the deny list: {unwelcome[-1].author} said AI help is unwelcome"
            )

        if status.merged:
            ledger.update_pull_request(
                pr["id"], state="merged", ci_status=ci, merged_at=_iso(status.merged_at)
            )
            _move(ledger, attempt_id, AttemptState.MERGED, "pull request merged")
            result.events.append("merged")
        elif status.state == "closed":
            note = feedback[-1].body[:500] if feedback else "closed without a comment"
            ledger.update_pull_request(
                pr["id"], state="closed", ci_status=ci, closed_at=_iso(status.closed_at), outcome_notes=note
            )
            _move(ledger, attempt_id, AttemptState.CLOSED, f"pull request closed without merge: {note[:200]}")
            until = now + timedelta(days=config.limits.cooldown_days_after_close)
            ledger.upsert_repository(repo, cooldown_until=until.isoformat(timespec="seconds"))
            result.events.append(f"closed without merge; {repo} in cooldown until {until.date().isoformat()}")
        else:
            ledger.update_pull_request(pr["id"], ci_status=ci)
            if state is AttemptState.SUBMITTED:
                ledger.transition(attempt_id, AttemptState.IN_REVIEW, "pull request seen open")
            for f in feedback:
                wants_close = f in unwelcome or asks_to_close(f.body)
                kind = "close" if wants_close else "reply"
                context = {
                    "author": f.author,
                    "body": f.body,
                    "at": f.created_at,
                    "pull_request": status.html_url,
                }
                body = "Thanks for taking a look. I'll close this." if wants_close else None
                if ledger.add_draft(attempt_id, kind, f.source, context, body):
                    result.events.append(f"{kind} draft for {f.source} from {f.author}")
            if ci == "failure":
                failing = sorted(r.name for r in runs if r.conclusion in FAILED)
                fix_context: dict[str, object] = {
                    "head": status.head_sha,
                    "failing": failing,
                    "pull_request": status.html_url,
                }
                if ledger.add_draft(attempt_id, "fix", f"ci:{status.head_sha}", fix_context):
                    result.events.append(f"fix draft for failing checks: {', '.join(failing)}")
        result.state = "merged" if status.merged else status.state
        results.append(result)
    return results


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
