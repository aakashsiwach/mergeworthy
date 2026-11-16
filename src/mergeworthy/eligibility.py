"""Eligibility checks from docs/policy.md sections 1 (repositories) and 2 (issues).

Every check returns a ``CheckResult`` with a status and a human-readable reason. ``UNCLEAR`` means the
repository or issue is skipped and listed for a human to decide. ``DEFERRED`` marks a check that can only run
later (in the sandbox) and does not block at this stage.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Any, Protocol

from mergeworthy.ai_policy import PolicyVerdict, classify
from mergeworthy.config import Config
from mergeworthy.github import (
    Comment,
    Commit,
    DirectoryEntry,
    FileContent,
    Issue,
    LinkedPullRequest,
    PullRequest,
    Repository,
)

MAINTAINER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
POLICY_DIRECTORIES = ("", ".github", "docs")
MAX_POLICY_FILES = 12


class GitHubReader(Protocol):
    """The read-only calls eligibility needs; implemented by ``GitHubClient`` and by test fakes."""

    def get_repository(self, full_name: str) -> Repository: ...
    def get_file(self, full_name: str, path: str, *, ref: str | None = None) -> FileContent | None: ...
    def list_directory(
        self, full_name: str, path: str = "", *, ref: str | None = None
    ) -> list[DirectoryEntry]: ...
    def list_commits(
        self, full_name: str, *, branch: str | None = None, since: datetime | None = None, limit: int = 30
    ) -> list[Commit]: ...
    def list_merged_pull_requests(self, full_name: str, *, scan_limit: int = 100) -> list[PullRequest]: ...
    def list_issue_comments(self, full_name: str, number: int, *, limit: int = 300) -> list[Comment]: ...
    def list_linked_pull_requests(self, full_name: str, number: int) -> list[LinkedPullRequest]: ...


def content_hash(parts: Iterable[tuple[str, str]]) -> str:
    """A stable SHA-256 over (name, text) pairs, used to tie a human decision to what it was based on."""
    digest = hashlib.sha256()
    for name, text in sorted(parts):
        digest.update(name.encode("utf-8") + b"\0" + text.encode("utf-8") + b"\0")
    return digest.hexdigest()


class Status(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNCLEAR = "unclear"
    DEFERRED = "deferred"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status in (Status.PASS, Status.DEFERRED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "reason": self.reason,
            "details": self.details,
        }


@dataclass(frozen=True)
class Report:
    subject: str
    results: list[CheckResult]

    @property
    def eligible(self) -> bool:
        return all(r.passed for r in self.results)

    @property
    def blocked(self) -> bool:
        """At least one check failed outright; a human decision would not change the outcome."""
        return any(r.status is Status.FAIL for r in self.results)

    @property
    def needs_human(self) -> bool:
        """Nothing failed outright, but at least one check needs a human decision."""
        return not self.blocked and any(r.status is Status.UNCLEAR for r in self.results)

    @property
    def failures(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]

    def get(self, name: str) -> CheckResult | None:
        return next((r for r in self.results if r.name == name), None)

    def summary(self) -> str:
        if self.eligible:
            return "eligible"
        reasons = "; ".join(f"{r.name}: {r.reason}" for r in self.failures)
        prefix = "needs a human decision" if self.needs_human else "not eligible"
        return f"{prefix} ({reasons})"

    def to_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.results]


def _pass(name: str, reason: str, **details: Any) -> CheckResult:
    return CheckResult(name, Status.PASS, reason, details)


def _fail(name: str, reason: str, **details: Any) -> CheckResult:
    return CheckResult(name, Status.FAIL, reason, details)


@cache
def osi_licenses() -> frozenset[str]:
    text = resources.files("mergeworthy").joinpath("data/osi_licenses.txt").read_text(encoding="utf-8")
    lines = (line.strip() for line in text.splitlines())
    return frozenset(line.lower() for line in lines if line and not line.startswith("#"))


def _is_bot(login: str | None) -> bool:
    return login is None or login.lower().endswith("[bot]")


# --- policy documents -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyDocuments:
    contributing: dict[str, str]
    code_of_conduct: dict[str, str]
    pull_request_templates: dict[str, str]
    ai_policies: dict[str, str]

    def all(self) -> dict[str, str]:
        return {
            **self.contributing,
            **self.code_of_conduct,
            **self.pull_request_templates,
            **self.ai_policies,
        }

    def content_hash(self) -> str:
        return content_hash(self.all().items())


def _kind(entry: DirectoryEntry) -> str | None:
    name = entry.name.lower()
    if entry.type == "file":
        if name.startswith("contributing"):
            return "contributing"
        if name.startswith(("code_of_conduct", "code-of-conduct")):
            return "code_of_conduct"
        if name.startswith("pull_request_template"):
            return "pull_request_template"
        if name.startswith(("ai_policy", "ai-policy")):
            return "ai_policy"
    elif entry.type == "dir" and name == "pull_request_template":
        return "pull_request_template_dir"
    return None


def fetch_policy_documents(gh: GitHubReader, repo: Repository) -> PolicyDocuments:
    """Read the contributing guide, code of conduct, pull request templates and ``AI_POLICY*`` files."""
    ref = repo.default_branch
    found: dict[str, list[str]] = {
        "contributing": [],
        "code_of_conduct": [],
        "pull_request_template": [],
        "ai_policy": [],
    }
    for directory in POLICY_DIRECTORIES:
        for entry in gh.list_directory(repo.full_name, directory, ref=ref):
            kind = _kind(entry)
            if kind == "pull_request_template_dir":
                files = gh.list_directory(repo.full_name, entry.path, ref=ref)
                found["pull_request_template"].extend(f.path for f in files if f.type == "file")
            elif kind is not None:
                found[kind].append(entry.path)

    budget = MAX_POLICY_FILES

    def read(paths: list[str]) -> dict[str, str]:
        nonlocal budget
        texts = {}
        for path in sorted(paths):
            if budget <= 0:
                break
            budget -= 1
            file = gh.get_file(repo.full_name, path, ref=ref)
            if file is not None:
                texts[path] = file.text
        return texts

    # AI policies first, so the file cap never drops them.
    ai = read(found["ai_policy"])
    contributing = read(found["contributing"])
    templates = read(found["pull_request_template"])
    conduct = read(found["code_of_conduct"])
    return PolicyDocuments(contributing, conduct, templates, ai)


# Phrases in a contributing guide that ask contributors to claim an issue before working on it.
_CLAIM_FIRST = re.compile(
    r"\b(?:ask(?:ed)?\s+(?:to\s+be|for\s+the\s+issue\s+to\s+be|before\s+being)\s+assigned"
    r"|(?:wait|until)\s+(?:to\s+be|you\s+are|you're|you\s+have\s+been)\s+assigned"
    r"|comment\s+on\s+the\s+issue\s+(?:first|before)"
    r"|(?:ask|check|discuss)\b[^.!?]{0,40}\bbefore\s+(?:you\s+)?(?:start|begin)\s+working"
    r"|claim\s+(?:an|the)\s+issue)",
    re.IGNORECASE,
)


def claim_required(contributing: Iterable[str]) -> bool:
    return any(_CLAIM_FIRST.search(" ".join(text.split())) for text in contributing)


# --- repository checks (policy.md section 1) --------------------------------------------------------------


def check_public(repo: Repository) -> CheckResult:
    if repo.private:
        return _fail("public", "repository is private")
    return _pass("public", "repository is public")


def check_not_archived(repo: Repository) -> CheckResult:
    if repo.archived:
        return _fail("not_archived", "repository is archived")
    return _pass("not_archived", "repository is not archived")


def check_license(repo: Repository) -> CheckResult:
    spdx = repo.license_spdx_id
    if not spdx or spdx == "NOASSERTION":
        return _fail("license", "no license that GitHub could identify")
    if spdx.lower() not in osi_licenses():
        return _fail("license", f"{spdx} is not on the list of OSI-approved licenses", spdx_id=spdx)
    return _pass("license", f"{spdx} is OSI-approved", spdx_id=spdx)


def check_language(repo: Repository, config: Config) -> CheckResult:
    language = (repo.language or "").lower()
    if language not in config.languages:
        return _fail("language", f"language {repo.language or 'unknown'} is not in scope")
    return _pass("language", f"language {repo.language} is in scope")


def check_recent_commit(gh: GitHubReader, repo: Repository, config: Config, now: datetime) -> CheckResult:
    days = config.eligibility.max_days_since_commit
    since = now - timedelta(days=days)
    commits = gh.list_commits(repo.full_name, branch=repo.default_branch, since=since, limit=1)
    if not commits:
        return _fail("recent_commit", f"no commit to {repo.default_branch} in the last {days} days")
    when = commits[0].committed_at
    return _pass(
        "recent_commit",
        f"commit to {repo.default_branch} within {days} days",
        last_commit=when.isoformat() if when else None,
    )


def check_outside_merge(gh: GitHubReader, repo: Repository, config: Config, now: datetime) -> CheckResult:
    days = config.eligibility.max_days_since_outside_merge
    cutoff = now - timedelta(days=days)
    for pr in gh.list_merged_pull_requests(repo.full_name):
        outside = pr.author_association not in MAINTAINER_ASSOCIATIONS and not _is_bot(pr.author)
        if outside and pr.merged_at is not None and pr.merged_at >= cutoff:
            return _pass(
                "outside_merge",
                f"pull request from an outside contributor merged within {days} days",
                pull_request=pr.html_url,
            )
    return _fail(
        "outside_merge", f"no pull request from an outside contributor merged in the last {days} days"
    )


def check_contributing_guide(documents: PolicyDocuments) -> CheckResult:
    if not documents.contributing:
        return _fail("contributing_guide", "no CONTRIBUTING file in the root, .github or docs")
    return _pass("contributing_guide", "contributing guide found", files=sorted(documents.contributing))


def check_ai_policy(documents: PolicyDocuments) -> CheckResult:
    result = classify(documents.all())
    details: dict[str, Any] = {
        "verdict": result.verdict.value,
        "sources": result.sources,
        "matches": [{"source": m.source, "quote": m.quote, "rule": m.note} for m in result.matches],
        "content_hash": documents.content_hash(),
    }
    first = result.matches[0] if result.matches else None
    if result.verdict is PolicyVerdict.FORBIDS and first:
        return _fail("ai_policy", f"{first.source} forbids or discourages AI contributions", **details)
    if result.verdict is PolicyVerdict.UNCLEAR and first:
        return CheckResult(
            "ai_policy",
            Status.UNCLEAR,
            f"{first.source} mentions AI without a clear policy; a human must decide",
            details,
        )
    if result.verdict is PolicyVerdict.PERMITS:
        details["disclosure"] = result.disclosure_quotes
        return _pass("ai_policy", "AI-assisted contributions are explicitly accepted", **details)
    return _pass("ai_policy", "no policy against AI-assisted contributions", **details)


def check_not_denied(repo: Repository, config: Config, ledger_denied: bool) -> CheckResult:
    if config.is_denied(repo.full_name) or ledger_denied:
        return _fail("not_denied", "repository is on the deny list")
    return _pass("not_denied", "repository is not on the deny list")


def check_cooldown(cooldown_until: datetime | None, now: datetime) -> CheckResult:
    if cooldown_until is not None and cooldown_until > now:
        return _fail("cooldown", f"in cooldown until {cooldown_until.date().isoformat()}")
    return _pass("cooldown", "not in cooldown")


def check_tests_runnable(config: Config) -> CheckResult:
    minutes = config.eligibility.max_test_minutes
    return CheckResult(
        "tests_in_sandbox",
        Status.DEFERRED,
        f"must run in the sandbox within {minutes} minutes; measured before work starts",
    )


def check_claim_first(documents: PolicyDocuments) -> CheckResult:
    required = claim_required(documents.contributing.values())
    if required:
        reason = "contributing guide asks to claim an issue first; a claim comment must be approved first"
    else:
        reason = "no claim step required"
    return _pass("claim_first", reason, required=required)


def check_repository(
    gh: GitHubReader,
    config: Config,
    full_name: str,
    *,
    now: datetime,
    ledger_denied: bool = False,
    cooldown_until: datetime | None = None,
) -> tuple[Repository, PolicyDocuments, Report]:
    repo = gh.get_repository(full_name)
    documents = fetch_policy_documents(gh, repo)
    checks: list[Callable[[], CheckResult]] = [
        lambda: check_public(repo),
        lambda: check_not_archived(repo),
        lambda: check_license(repo),
        lambda: check_language(repo, config),
        lambda: check_recent_commit(gh, repo, config, now),
        lambda: check_outside_merge(gh, repo, config, now),
        lambda: check_contributing_guide(documents),
        lambda: check_ai_policy(documents),
        lambda: check_not_denied(repo, config, ledger_denied),
        lambda: check_cooldown(cooldown_until, now),
        lambda: check_tests_runnable(config),
        lambda: check_claim_first(documents),
    ]
    return repo, documents, Report(repo.full_name, [check() for check in checks])


# --- issue checks (policy.md section 2) -------------------------------------------------------------------


_WORKING_ON_IT = re.compile(
    r"(?:\b(?:i'?m|i\s+am|i'?ll\s+be|currently)\s+(?:already\s+)?working\s+on\s+(?:this|it|a\s+fix|a\s+pr)\b"
    r"|\bi(?:'?d|\s+would)\s+(?:like|love)\s+to\s+(?:work\s+on|take|pick\s+up|tackle)\b"
    r"|\b(?:can|could|may)\s+i\s+(?:work\s+on|take|pick\s+up|tackle|be\s+assigned)\b"
    r"|\b(?:i'?ll|i\s+will|let\s+me)\s+(?:take|work\s+on|pick\s+up|handle|tackle)\s+(?:this|it)\b"
    r"|\bassign\s+(?:this\s+|it\s+)?(?:to\s+)?me\b"
    r"|^/assign\b)",
    re.IGNORECASE | re.MULTILINE,
)

_REPRODUCIBLE = re.compile(
    r"(?:steps\s+to\s+reproduce|to\s+reproduce|reproduc|expected\s+(?:behaviou?r|result|output)"
    r"|actual\s+(?:behaviou?r|result|output)|traceback|stack\s*trace|exception|```"
    r"|acceptance\s+criteria|proposed\s+(?:change|solution)|should\s+(?:be|return|raise|accept|support))",
    re.IGNORECASE,
)

MIN_BODY_CHARS = 80
DETAILED_BODY_CHARS = 400


def check_label(issue: Issue, config: Config) -> CheckResult:
    wanted = {label.lower() for label in config.labels}
    matching = [label for label in issue.labels if label.lower() in wanted]
    if not matching:
        return _fail("help_label", f"none of the labels {sorted(config.labels)}")
    return _pass("help_label", f"labelled {', '.join(matching)}")


def check_open(issue: Issue) -> CheckResult:
    if issue.state != "open":
        return _fail("open", f"issue is {issue.state}")
    return _pass("open", "issue is open")


def check_unassigned(issue: Issue) -> CheckResult:
    if issue.assignees:
        return _fail("unassigned", f"assigned to {', '.join(issue.assignees)}")
    return _pass("unassigned", "not assigned")


def check_no_open_pull_request(gh: GitHubReader, issue: Issue) -> CheckResult:
    open_prs = [
        pr for pr in gh.list_linked_pull_requests(issue.repository, issue.number) if pr.state == "open"
    ]
    if open_prs:
        return _fail(
            "no_open_pull_request",
            f"linked open pull request {open_prs[0].html_url}",
            pull_requests=[pr.html_url for pr in open_prs],
        )
    return _pass("no_open_pull_request", "no linked open pull request")


def check_no_recent_claim(gh: GitHubReader, issue: Issue, config: Config, now: datetime) -> CheckResult:
    days = config.eligibility.claim_comment_days
    cutoff = now - timedelta(days=days)
    for comment in gh.list_issue_comments(issue.repository, issue.number):
        if comment.created_at < cutoff or _is_bot(comment.author):
            continue
        if _WORKING_ON_IT.search(comment.body.replace("\u2019", "'")):
            return _fail(
                "no_recent_claim",
                f"{comment.author} said they are working on it within {days} days",
                comment_id=comment.id,
            )
    return _pass("no_recent_claim", f"nobody claimed it in the last {days} days")


def check_specific(issue: Issue) -> CheckResult:
    body = issue.body.strip()
    if len(body) < MIN_BODY_CHARS:
        return _fail("specific", "description is too short to verify a fix")
    if _REPRODUCIBLE.search(body) or len(body) >= DETAILED_BODY_CHARS:
        return _pass("specific", "describes reproduction steps or a concrete change")
    return CheckResult(
        "specific",
        Status.UNCLEAR,
        "no reproduction steps or concrete change found; a human must decide",
        {"content_hash": content_hash([("title", issue.title), ("body", issue.body)])},
    )


def check_issue(gh: GitHubReader, config: Config, issue: Issue, *, now: datetime) -> Report:
    checks: list[Callable[[], CheckResult]] = [
        lambda: check_label(issue, config),
        lambda: check_open(issue),
        lambda: check_unassigned(issue),
        lambda: check_no_open_pull_request(gh, issue),
        lambda: check_no_recent_claim(gh, issue, config, now),
        lambda: check_specific(issue),
    ]
    return Report(f"{issue.repository}#{issue.number}", [check() for check in checks])


# --- human decisions --------------------------------------------------------------------------------------


# (subject, check name, content hash) -> (decision "allow" or "deny", reason), or None if not decided.
DecisionLookup = Callable[[str, str, str], tuple[str, str] | None]


def apply_decisions(report: Report, lookup: DecisionLookup) -> Report:
    """Replace unclear results with the human's decision for the same content, where one exists."""
    results = []
    for result in report.results:
        digest = result.details.get("content_hash")
        if result.status is Status.UNCLEAR and isinstance(digest, str):
            found = lookup(report.subject, result.name, digest)
            if found is not None:
                decision, reason = found
                status = Status.PASS if decision == "allow" else Status.FAIL
                verb = "allowed" if decision == "allow" else "denied"
                result = CheckResult(
                    result.name,
                    status,
                    f"a human {verb} this: {reason}",
                    {**result.details, "human": decision},
                )
        results.append(result)
    return Report(report.subject, results)
