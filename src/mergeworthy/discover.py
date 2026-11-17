"""Find candidate issues, record them in the ledger, and record eligibility results.

Only read-only GitHub calls are made here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mergeworthy.config import Config
from mergeworthy.eligibility import (
    DecisionLookup,
    GitHubReader,
    Report,
    Status,
    apply_decisions,
    check_issue,
    check_repository,
)
from mergeworthy.github import Issue
from mergeworthy.ledger import TERMINAL_STATES, AttemptState, Decision, DecisionRecord, Ledger


class DiscoveryReader(GitHubReader, Protocol):
    def search_issues(self, label: str, language: str, *, limit: int = 100) -> list[Issue]: ...
    def get_issue(self, full_name: str, number: int) -> Issue: ...


@dataclass(frozen=True)
class Discovered:
    issue: Issue
    language: str
    issue_id: int
    attempt_id: int | None
    new: bool


def discover(
    gh: DiscoveryReader,
    ledger: Ledger,
    config: Config,
    *,
    languages: list[str] | None = None,
    limit: int = 30,
) -> list[Discovered]:
    """Search each language and label, store new candidates and open one attempt per new issue.

    Repositories on the deny list (configuration or ledger) are left out. ``limit`` caps the number of
    candidates returned per language.
    """
    selected = languages or config.languages
    unknown = sorted(set(selected) - set(config.languages))
    if unknown:
        raise ValueError(f"languages not enabled in the configuration: {unknown}")
    results: list[Discovered] = []
    seen: set[str] = set()
    for language in selected:
        count = 0
        for label in config.labels:
            if count >= limit:
                break
            for issue in gh.search_issues(label, language, limit=limit):
                if count >= limit:
                    break
                if issue.html_url in seen or config.is_denied(issue.repository):
                    continue
                record = ledger.get_repository(issue.repository)
                if record is not None and record["deny_listed"]:
                    continue
                seen.add(issue.html_url)
                results.append(_store(ledger, config, issue, language))
                count += 1
    return results


def _store(ledger: Ledger, config: Config, issue: Issue, language: str) -> Discovered:
    repo_id = ledger.upsert_repository(issue.repository, language=language)
    issue_id = ledger.upsert_issue(repo_id, issue.number, issue.title, issue.labels, issue.html_url)
    existing = ledger.attempts_for_issue(issue_id)
    if existing:
        return Discovered(issue, language, issue_id, existing[-1].id, new=False)
    if len(existing) >= config.limits.attempts_per_issue:
        return Discovered(issue, language, issue_id, None, new=False)
    attempt_id = ledger.create_attempt(issue_id, f"found by search for {language} issues")
    return Discovered(issue, language, issue_id, attempt_id, new=True)


# --- check ------------------------------------------------------------------------------------------------

_ISSUE_URL = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s]+)/issues/(\d+)/?(?:[?#].*)?$")
_ISSUE_REF = re.compile(r"^([^/\s#]+/[^/\s#]+)#(\d+)$")
_REPO_URL = re.compile(r"^https?://github\.com/([^/\s]+/[^/\s#?]+?)(?:\.git)?/?$")
_REPO_REF = re.compile(r"^([^/\s#]+/[^/\s#]+)$")


def parse_target(target: str) -> tuple[str, int | None]:
    """Parse ``owner/repo``, ``owner/repo#12``, or a GitHub repository or issue URL."""
    value = target.strip()
    for pattern in (_ISSUE_URL, _ISSUE_REF):
        m = pattern.match(value)
        if m:
            return m.group(1), int(m.group(2))
    for pattern in (_REPO_URL, _REPO_REF):
        m = pattern.match(value)
        if m:
            return m.group(1), None
    raise ValueError(f"expected owner/repo, owner/repo#number or a GitHub URL, got {target!r}")


@dataclass(frozen=True)
class CheckOutcome:
    repository: Report
    issue: Report | None
    attempt_id: int | None
    attempt_state: AttemptState | None

    @property
    def reports(self) -> list[Report]:
        return [self.repository] + ([self.issue] if self.issue is not None else [])

    @property
    def eligible(self) -> bool:
        return all(r.eligible for r in self.reports)

    @property
    def blocked(self) -> bool:
        return any(r.blocked for r in self.reports)

    @property
    def needs_human(self) -> bool:
        """Nothing failed outright, but a human must decide at least one check."""
        return not self.blocked and not self.eligible


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _lookup(ledger: Ledger) -> DecisionLookup:
    def lookup(subject: str, check_name: str, digest: str) -> tuple[str, str] | None:
        found = ledger.find_decision(subject, check_name, digest)
        if found is None or found.decision is None:
            return None
        return found.decision.value, found.reason or ""

    return lookup


def _request_decisions(ledger: Ledger, report: Report) -> None:
    for result in report.results:
        digest = result.details.get("content_hash")
        if result.status is Status.UNCLEAR and isinstance(digest, str):
            ledger.request_decision(report.subject, result.name, digest, result.reason)


def _next_state(outcome: CheckOutcome) -> AttemptState:
    if outcome.eligible:
        return AttemptState.ELIGIBLE
    if outcome.needs_human:
        return AttemptState.NEEDS_HUMAN
    return AttemptState.SKIPPED


def _reasons(outcome: CheckOutcome) -> str:
    if outcome.eligible:
        return "all eligibility checks passed"
    return "; ".join(f"{r.name}: {r.reason}" for report in outcome.reports for r in report.failures)


def check_and_record(
    gh: DiscoveryReader,
    ledger: Ledger,
    config: Config,
    target: str,
    *,
    now: datetime,
) -> CheckOutcome:
    """Run eligibility checks, apply stored human decisions and record the results.

    Unclear checks without a decision for the same content become open questions for a human. For an
    issue, an attempt in ``discovered`` moves to ``eligible``, ``needs_human`` or ``skipped``; an attempt
    in ``needs_human`` moves to ``eligible`` or ``skipped`` once the questions are answered. Attempts in
    any other state are left alone.
    """
    full_name, number = parse_target(target)
    record = ledger.get_repository(full_name)
    repo, _documents, raw_repo_report = check_repository(
        gh,
        config,
        full_name,
        now=now,
        ledger_denied=bool(record and record["deny_listed"]),
        cooldown_until=_parse_time(record["cooldown_until"]) if record else None,
    )
    repo_report = apply_decisions(raw_repo_report, _lookup(ledger))
    _request_decisions(ledger, repo_report)
    by_name = {r.name: r.to_dict() for r in repo_report.results}
    activity = {k: by_name[k] for k in ("recent_commit", "outside_merge", "tests_in_sandbox")}
    policy: dict[str, object] = {
        k: by_name[k] for k in ("contributing_guide", "ai_policy", "claim_first", "license")
    }
    policy["needs_human"] = repo_report.needs_human
    repo_id = ledger.upsert_repository(
        repo.full_name,
        language=(repo.language or "").lower() or None,
        license=repo.license_spdx_id,
        default_branch=repo.default_branch,
        activity_checks=activity,
        policy_check=policy,
    )
    if number is None:
        return CheckOutcome(repo_report, None, None, None)

    issue = gh.get_issue(repo.full_name, number)
    issue_report = apply_decisions(check_issue(gh, config, issue, now=now), _lookup(ledger))
    _request_decisions(ledger, issue_report)
    issue_id = ledger.upsert_issue(repo_id, issue.number, issue.title, issue.labels, issue.html_url)
    outcome = CheckOutcome(repo_report, issue_report, None, None)
    ledger.set_issue_eligibility(issue_id, outcome.eligible, repo_report.to_dicts() + issue_report.to_dicts())

    attempts = ledger.attempts_for_issue(issue_id)
    if attempts:
        attempt = attempts[-1]
    else:
        attempt = ledger.get_attempt(ledger.create_attempt(issue_id, "checked by request"))
    state = attempt.state
    target_state = _next_state(outcome)
    movable = state is AttemptState.DISCOVERED or (
        state is AttemptState.NEEDS_HUMAN and target_state is not AttemptState.NEEDS_HUMAN
    )
    if movable:
        state = ledger.transition(attempt.id, target_state, _reasons(outcome)).state
    return CheckOutcome(repo_report, issue_report, attempt.id, state)


@dataclass(frozen=True)
class DecisionOutcome:
    record: DecisionRecord
    skipped_attempts: list[int]
    abandoned_attempts: list[int]
    resumable_attempts: list[int]


def record_decision(
    ledger: Ledger, subject: str, check_name: str, decision: Decision, reason: str
) -> DecisionOutcome:
    """Answer an open question and resolve every attempt that was waiting on it.

    - Deny: attempts in ``needs_human`` move to ``skipped``; attempts paused mid-way (for example by the
      re-check before work or submission) move to ``abandoned``.
    - Allow: attempts in ``needs_human`` move on at the next ``check``, which re-runs the other checks first;
      paused attempts are resumable, because the re-check reuses this decision while the content is unchanged.
    """
    full_name, number = parse_target(subject)
    normalised = full_name if number is None else f"{full_name}#{number}"
    record = ledger.decide(normalised, check_name, decision, reason)
    skipped: list[int] = []
    abandoned: list[int] = []
    resumable: list[int] = []
    for attempt in ledger.attempts_for_subject(normalised):
        if attempt.state in TERMINAL_STATES or attempt.state is AttemptState.DISCOVERED:
            continue
        waiting = attempt.state is AttemptState.NEEDS_HUMAN
        if decision is Decision.DENY:
            target = AttemptState.SKIPPED if waiting else AttemptState.ABANDONED
            ledger.transition(attempt.id, target, f"a human denied {check_name}: {reason}")
            (skipped if waiting else abandoned).append(attempt.id)
        elif not waiting:
            resumable.append(attempt.id)
    return DecisionOutcome(record, skipped, abandoned, resumable)
