"""Find candidate issues, record them in the ledger, and record eligibility results.

Only read-only GitHub calls are made here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from mergeworthy.config import Config
from mergeworthy.eligibility import GitHubReader, Report, check_issue, check_repository
from mergeworthy.github import Issue
from mergeworthy.ledger import AttemptState, Ledger


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
    def eligible(self) -> bool:
        return self.repository.eligible and (self.issue is None or self.issue.eligible)

    @property
    def needs_human(self) -> bool:
        return self.repository.needs_human or (self.issue is not None and self.issue.needs_human)


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def check_and_record(
    gh: DiscoveryReader,
    ledger: Ledger,
    config: Config,
    target: str,
    *,
    now: datetime,
) -> CheckOutcome:
    """Run eligibility checks and record the results.

    For an issue, the issue's attempt moves from ``discovered`` to ``eligible`` or ``skipped``. Attempts in
    any other state are left alone.
    """
    full_name, number = parse_target(target)
    record = ledger.get_repository(full_name)
    repo, _documents, repo_report = check_repository(
        gh,
        config,
        full_name,
        now=now,
        ledger_denied=bool(record and record["deny_listed"]),
        cooldown_until=_parse_time(record["cooldown_until"]) if record else None,
    )
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
    issue_report = check_issue(gh, config, issue, now=now)
    issue_id = ledger.upsert_issue(repo_id, issue.number, issue.title, issue.labels, issue.html_url)
    eligible = repo_report.eligible and issue_report.eligible
    ledger.set_issue_eligibility(issue_id, eligible, repo_report.to_dicts() + issue_report.to_dicts())

    attempts = ledger.attempts_for_issue(issue_id)
    if attempts:
        attempt = attempts[-1]
    else:
        attempt = ledger.get_attempt(ledger.create_attempt(issue_id, "checked by request"))
    state = attempt.state
    if state is AttemptState.DISCOVERED:
        if eligible:
            reason = "all eligibility checks passed"
            state = ledger.transition(attempt.id, AttemptState.ELIGIBLE, reason).state
        else:
            reasons = "; ".join(f"{r.name}: {r.reason}" for r in repo_report.failures + issue_report.failures)
            needs_human = repo_report.needs_human or issue_report.needs_human
            reason = ("needs human decision: " if needs_human else "") + reasons
            state = ledger.transition(attempt.id, AttemptState.SKIPPED, reason).state
    return CheckOutcome(repo_report, issue_report, attempt.id, state)
