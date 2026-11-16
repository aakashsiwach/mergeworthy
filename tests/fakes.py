"""An in-memory, read-only GitHub for eligibility and discovery tests. No network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from mergeworthy.github import (
    Comment,
    Commit,
    DirectoryEntry,
    FileContent,
    Issue,
    LinkedPullRequest,
    NotFound,
    PullRequest,
    PullRequestMention,
    Repository,
)

FIXTURES = Path(__file__).parent / "fixtures" / "repos"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def days_ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def make_repository(full_name: str, **overrides: Any) -> Repository:
    data: dict[str, Any] = {
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "private": False,
        "archived": False,
        "fork": False,
        "language": "Python",
        "license_spdx_id": "MIT",
        "default_branch": "main",
        "pushed_at": days_ago(1),
    }
    data.update(overrides)
    return Repository(**data)


def make_issue(full_name: str, number: int = 1, **overrides: Any) -> Issue:
    data: dict[str, Any] = {
        "repository": full_name,
        "number": number,
        "title": f"Crash when parsing empty input ({number})",
        "body": (
            "Steps to reproduce:\n1. Call `parse('')`\n2. See the traceback\n\n"
            "Expected behaviour: an empty result instead of an exception."
        ),
        "state": "open",
        "labels": ["good first issue", "bug"],
        "assignees": [],
        "author": "reporter",
        "html_url": f"https://github.com/{full_name}/issues/{number}",
        "comments": 0,
        "created_at": days_ago(10),
        "updated_at": days_ago(2),
    }
    data.update(overrides)
    return Issue(**data)


def outside_merge(days: float = 5, association: str = "CONTRIBUTOR", author: str = "outsider") -> PullRequest:
    return PullRequest(
        number=100,
        state="closed",
        author=author,
        author_association=association,
        html_url="https://github.com/x/y/pull/100",
        created_at=days_ago(days + 1),
        merged_at=days_ago(days),
        closed_at=days_ago(days),
    )


@dataclass
class FakeRepo:
    repository: Repository
    files: dict[str, str] = field(default_factory=dict)
    commits: list[Commit] = field(default_factory=list)
    merged: list[PullRequest] = field(default_factory=list)
    issues: dict[int, Issue] = field(default_factory=dict)
    comments: dict[int, list[Comment]] = field(default_factory=dict)
    linked: dict[int, list[LinkedPullRequest]] = field(default_factory=dict)
    mentions: dict[int, list[PullRequestMention]] = field(default_factory=dict)


def load_fixture(name: str, full_name: str | None = None, **repo_overrides: Any) -> FakeRepo:
    """A healthy, active repository whose files come from ``tests/fixtures/repos/<name>/files``."""
    root = FIXTURES / name / "files"
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()
    }
    full_name = full_name or f"fixture/{name.replace('_', '-')}"
    return FakeRepo(
        repository=make_repository(full_name, **repo_overrides),
        files=files,
        commits=[Commit(sha="c0ffee", author="maintainer", committed_at=days_ago(3))],
        merged=[outside_merge()],
    )


class FakeGitHub:
    def __init__(self, *repos: FakeRepo) -> None:
        self.repos = {r.repository.full_name.lower(): r for r in repos}
        self.searches: dict[tuple[str, str], list[Issue]] = {}
        self.calls: list[str] = []

    def _repo(self, full_name: str) -> FakeRepo:
        try:
            return self.repos[full_name.lower()]
        except KeyError:
            raise NotFound(f"{full_name} not found", 404) from None

    def search_issues(self, label: str, language: str, *, limit: int = 100) -> list[Issue]:
        self.calls.append(f"search {label} {language}")
        return self.searches.get((label, language), [])[:limit]

    def get_repository(self, full_name: str) -> Repository:
        return self._repo(full_name).repository

    def get_issue(self, full_name: str, number: int) -> Issue:
        try:
            return self._repo(full_name).issues[number]
        except KeyError:
            raise NotFound(f"{full_name}#{number} not found", 404) from None

    def get_file(self, full_name: str, path: str, *, ref: str | None = None) -> FileContent | None:
        text = self._repo(full_name).files.get(path.strip("/"))
        return None if text is None else FileContent(path=path, sha="0", text=text)

    def list_directory(
        self, full_name: str, path: str = "", *, ref: str | None = None
    ) -> list[DirectoryEntry]:
        prefix = f"{path.strip('/')}/" if path.strip("/") else ""
        entries: dict[str, DirectoryEntry] = {}
        for file_path in self._repo(full_name).files:
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            name, _, below = rest.partition("/")
            kind = "dir" if below else "file"
            entries[name] = DirectoryEntry(name=name, path=prefix + name, type=kind)
        return list(entries.values())

    def list_commits(
        self, full_name: str, *, branch: str | None = None, since: datetime | None = None, limit: int = 30
    ) -> list[Commit]:
        commits = self._repo(full_name).commits
        if since is not None:
            commits = [c for c in commits if c.committed_at is not None and c.committed_at >= since]
        return commits[:limit]

    def list_merged_pull_requests(self, full_name: str, *, scan_limit: int = 100) -> list[PullRequest]:
        return self._repo(full_name).merged

    def list_issue_comments(self, full_name: str, number: int, *, limit: int = 300) -> list[Comment]:
        return self._repo(full_name).comments.get(number, [])

    def list_linked_pull_requests(self, full_name: str, number: int) -> list[LinkedPullRequest]:
        return self._repo(full_name).linked.get(number, [])

    def search_open_pull_requests_mentioning(
        self, full_name: str, number: int, *, limit: int = 50
    ) -> list[PullRequestMention]:
        return self._repo(full_name).mentions.get(number, [])[:limit]

    def close(self) -> None:
        pass
