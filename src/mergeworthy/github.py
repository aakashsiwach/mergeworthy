"""Read-only GitHub REST client.

Only GET requests are possible through this client. Write operations (fork, push, comment, pull request)
belong to ``submit.py`` and sit behind the allowlist, the ``approved`` state and dry-run.

Rate limits follow GitHub's REST best practices: honour ``retry-after``; when ``x-ratelimit-remaining`` is
``0`` wait until ``x-ratelimit-reset``; otherwise wait at least a minute and back off exponentially, giving up
after a fixed number of retries.
"""

from __future__ import annotations

import base64
import binascii
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, Self
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
SEARCH_RESULT_CAP = 1000
MAX_PER_PAGE = 100

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class NotFound(GitHubError):
    pass


class RateLimited(GitHubError):
    pass


# --- models -----------------------------------------------------------------------------------------------


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Repository(_Model):
    full_name: str
    html_url: str
    private: bool
    archived: bool
    fork: bool
    language: str | None
    license_spdx_id: str | None
    default_branch: str
    pushed_at: datetime | None


class Issue(_Model):
    repository: str
    number: int
    title: str
    body: str
    state: str
    labels: list[str]
    assignees: list[str]
    author: str | None
    html_url: str
    comments: int
    created_at: datetime
    updated_at: datetime


class Comment(_Model):
    id: int
    author: str | None
    author_association: str
    body: str
    created_at: datetime


class Commit(_Model):
    sha: str
    author: str | None
    committed_at: datetime | None


class PullRequest(_Model):
    number: int
    state: str
    author: str | None
    author_association: str
    html_url: str
    created_at: datetime
    merged_at: datetime | None
    closed_at: datetime | None


class LinkedPullRequest(_Model):
    repository: str
    number: int
    state: str
    merged: bool
    html_url: str


class FileContent(_Model):
    path: str
    sha: str
    text: str


class DirectoryEntry(_Model):
    name: str
    path: str
    type: str


class PullRequestMention(_Model):
    """An open pull request found by text search; whether it really refers to the issue is decided later."""

    number: int
    title: str
    body: str
    html_url: str


def _login(user: Any) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _repo_from_api_url(url: str) -> str:
    # e.g. https://api.github.com/repos/owner/name
    parts = url.rstrip("/").split("/")
    return f"{parts[-2]}/{parts[-1]}"


def _repository(data: dict[str, Any]) -> Repository:
    license_info = data.get("license") or {}
    return Repository(
        full_name=data["full_name"],
        html_url=data["html_url"],
        private=data.get("private", False),
        archived=data.get("archived", False),
        fork=data.get("fork", False),
        language=data.get("language"),
        license_spdx_id=license_info.get("spdx_id"),
        default_branch=data["default_branch"],
        pushed_at=data.get("pushed_at"),
    )


def _issue(data: dict[str, Any]) -> Issue:
    return Issue(
        repository=_repo_from_api_url(data["repository_url"]),
        number=data["number"],
        title=data["title"],
        body=data.get("body") or "",
        state=data["state"],
        labels=[label["name"] if isinstance(label, dict) else str(label) for label in data.get("labels", [])],
        assignees=[a["login"] for a in data.get("assignees") or []],
        author=_login(data.get("user")),
        html_url=data["html_url"],
        comments=data.get("comments", 0),
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )


def _comment(data: dict[str, Any]) -> Comment:
    return Comment(
        id=data["id"],
        author=_login(data.get("user")),
        author_association=data.get("author_association", "NONE"),
        body=data.get("body") or "",
        created_at=data["created_at"],
    )


def _commit(data: dict[str, Any]) -> Commit:
    committer = (data.get("commit") or {}).get("committer") or {}
    return Commit(sha=data["sha"], author=_login(data.get("author")), committed_at=committer.get("date"))


def _pull_request(data: dict[str, Any]) -> PullRequest:
    return PullRequest(
        number=data["number"],
        state=data["state"],
        author=_login(data.get("user")),
        author_association=data.get("author_association", "NONE"),
        html_url=data["html_url"],
        created_at=data["created_at"],
        merged_at=data.get("merged_at"),
        closed_at=data.get("closed_at"),
    )


def _check_repo(full_name: str) -> str:
    if not _REPO_NAME.match(full_name):
        raise ValueError(f"not a repository name in owner/name form: {full_name!r}")
    return full_name


# --- client -----------------------------------------------------------------------------------------------


class GitHubClient:
    """A read-only GitHub REST client with rate-limit handling, pagination and timeouts."""

    def __init__(
        self,
        token: SecretStr | None = None,
        *,
        base_url: str = API_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        max_wait_seconds: float = 900.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "mergeworthy",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        self._http = httpx.Client(
            base_url=base_url,
            headers=headers,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 10.0)),
            follow_redirects=True,
        )
        self._max_retries = max_retries
        self._max_wait = max_wait_seconds
        self._sleep = sleep
        self._clock = clock
        self._blocked_until: float | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # transport

    def _wait(self, seconds: float) -> None:
        if seconds > self._max_wait:
            raise RateLimited(f"rate limited for {seconds:.0f}s, longer than the {self._max_wait:.0f}s limit")
        if seconds > 0:
            self._sleep(seconds)

    def _reset_delay(self, response: httpx.Response) -> float | None:
        reset = response.headers.get("x-ratelimit-reset")
        if reset is None:
            return None
        try:
            return max(0.0, float(reset) - self._clock()) + 1.0
        except ValueError:
            return None

    def _rate_limit_delay(self, response: httpx.Response, retry: int) -> float | None:
        """Seconds to wait before retrying, or None if the response is not a rate-limit error."""
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        if response.headers.get("x-ratelimit-remaining") == "0":
            delay = self._reset_delay(response)
            if delay is not None:
                return delay
        if response.status_code == 429 or "rate limit" in response.text.lower():
            return 60.0 * (2.0**retry)
        return None

    def _get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        if self._blocked_until is not None:
            self._wait(self._blocked_until - self._clock())
            self._blocked_until = None
        for retry in range(self._max_retries + 1):
            try:
                response = self._http.get(url, params=params)
            except httpx.TimeoutException as exc:
                if retry == self._max_retries:
                    raise GitHubError(f"GET {url} timed out") from exc
                self._sleep(2.0**retry)
                continue
            delay = self._rate_limit_delay(response, retry)
            if delay is None:
                break
            if retry == self._max_retries:
                raise RateLimited(
                    f"GET {url}: still rate limited after {retry} retries", response.status_code
                )
            self._wait(delay)
        if response.headers.get("x-ratelimit-remaining") == "0":
            delay = self._reset_delay(response)
            if delay is not None:
                self._blocked_until = self._clock() + delay
        if response.status_code == 404:
            raise NotFound(f"GET {url}: not found", 404)
        if response.status_code >= 400:
            try:
                message = response.json().get("message", "")
            except ValueError:
                message = response.text[:200]
            raise GitHubError(f"GET {url}: HTTP {response.status_code} {message}", response.status_code)
        return response

    def _get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        return self._get(url, params).json()

    def _paginate(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int | None = None,
        items_key: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield items across pages by following ``Link: rel="next"``, stopping after ``limit`` items."""
        query = dict(params or {})
        query.setdefault("per_page", MAX_PER_PAGE if limit is None else min(MAX_PER_PAGE, max(1, limit)))
        count = 0
        next_url: str | None = url
        next_params: dict[str, Any] | None = query
        while next_url is not None:
            response = self._get(next_url, next_params)
            data = response.json()
            items = data[items_key] if items_key else data
            for item in items:
                if limit is not None and count >= limit:
                    return
                yield item
                count += 1
            next_url = response.links.get("next", {}).get("url")
            next_params = None  # the next link already carries the query

    # API calls

    def search_issues(self, label: str, language: str, *, limit: int = 100) -> list[Issue]:
        """Open, unassigned issues with ``label`` in non-archived repositories of ``language``.

        One label per query, because several ``label:`` qualifiers would all be required.
        """
        query = f'is:issue is:open no:assignee archived:false label:"{label}" language:{language}'
        items = self._paginate(
            "/search/issues",
            {"q": query, "sort": "updated", "order": "desc"},
            limit=min(limit, SEARCH_RESULT_CAP),
            items_key="items",
        )
        return [_issue(item) for item in items if "pull_request" not in item]

    def search_open_pull_requests_mentioning(
        self, full_name: str, number: int, *, limit: int = 50
    ) -> list[PullRequestMention]:
        """Open pull requests in the repository whose title or body contains the issue number."""
        query = f"repo:{_check_repo(full_name)} is:pr is:open {int(number)} in:title,body"
        items = self._paginate("/search/issues", {"q": query}, limit=limit, items_key="items")
        return [
            PullRequestMention(
                number=item["number"],
                title=item.get("title") or "",
                body=item.get("body") or "",
                html_url=item["html_url"],
            )
            for item in items
            if "pull_request" in item
        ]

    def get_repository(self, full_name: str) -> Repository:
        return _repository(self._get_json(f"/repos/{_check_repo(full_name)}"))

    def get_issue(self, full_name: str, number: int) -> Issue:
        data = self._get_json(f"/repos/{_check_repo(full_name)}/issues/{int(number)}")
        if "pull_request" in data:
            raise GitHubError(f"{full_name}#{number} is a pull request, not an issue")
        return _issue(data)

    def get_file(self, full_name: str, path: str, *, ref: str | None = None) -> FileContent | None:
        """A file's text, or None if it does not exist or is not a file."""
        url = f"/repos/{_check_repo(full_name)}/contents/{quote(path.strip('/'), safe='/')}"
        try:
            data = self._get_json(url, {"ref": ref} if ref else None)
        except NotFound:
            return None
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        if data.get("encoding") != "base64":
            raise GitHubError(f"{full_name}:{path} has no inline content (encoding {data.get('encoding')!r})")
        try:
            raw = base64.b64decode(data.get("content", ""))
        except binascii.Error as exc:
            raise GitHubError(f"{full_name}:{path} has invalid base64 content") from exc
        return FileContent(path=data["path"], sha=data["sha"], text=raw.decode("utf-8", errors="replace"))

    def list_directory(
        self, full_name: str, path: str = "", *, ref: str | None = None
    ) -> list[DirectoryEntry]:
        """Entries of a directory, or an empty list if it does not exist."""
        url = f"/repos/{_check_repo(full_name)}/contents/{quote(path.strip('/'), safe='/')}"
        try:
            data = self._get_json(url.rstrip("/"), {"ref": ref} if ref else None)
        except NotFound:
            return []
        if not isinstance(data, list):
            return []
        return [DirectoryEntry(name=e["name"], path=e["path"], type=e["type"]) for e in data]

    def list_commits(
        self,
        full_name: str,
        *,
        branch: str | None = None,
        since: datetime | None = None,
        limit: int = 30,
    ) -> list[Commit]:
        params: dict[str, Any] = {}
        if branch:
            params["sha"] = branch
        if since:
            params["since"] = since.isoformat()
        items = self._paginate(f"/repos/{_check_repo(full_name)}/commits", params, limit=limit)
        return [_commit(item) for item in items]

    def list_merged_pull_requests(self, full_name: str, *, scan_limit: int = 100) -> list[PullRequest]:
        """Merged pull requests among the ``scan_limit`` most recently updated closed ones."""
        items = self._paginate(
            f"/repos/{_check_repo(full_name)}/pulls",
            {"state": "closed", "sort": "updated", "direction": "desc"},
            limit=scan_limit,
        )
        return [pr for pr in (_pull_request(item) for item in items) if pr.merged_at is not None]

    def list_issue_comments(self, full_name: str, number: int, *, limit: int = 300) -> list[Comment]:
        items = self._paginate(f"/repos/{_check_repo(full_name)}/issues/{int(number)}/comments", limit=limit)
        return [_comment(item) for item in items]

    def list_linked_pull_requests(self, full_name: str, number: int) -> list[LinkedPullRequest]:
        """Pull requests that reference the issue, from ``cross-referenced`` timeline events."""
        events = self._paginate(f"/repos/{_check_repo(full_name)}/issues/{int(number)}/timeline")
        found: dict[str, LinkedPullRequest] = {}
        for event in events:
            if event.get("event") != "cross-referenced":
                continue
            source = (event.get("source") or {}).get("issue") or {}
            pull = source.get("pull_request")
            if not isinstance(pull, dict):
                continue
            linked = LinkedPullRequest(
                repository=_repo_from_api_url(source["repository_url"]),
                number=source["number"],
                state=source["state"],
                merged=pull.get("merged_at") is not None,
                html_url=source["html_url"],
            )
            found[linked.html_url] = linked
        return list(found.values())
