import base64
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr

from mergeworthy.github import (
    API_URL,
    GitHubClient,
    GitHubError,
    NotFound,
    RateLimited,
)

REPO = "owner/repo"
TOKEN = "FAKE_TOKEN_FOR_TESTS"


class FakeTime:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def client(fake_time: FakeTime) -> Iterator[GitHubClient]:
    with GitHubClient(SecretStr(TOKEN), sleep=fake_time.sleep, clock=fake_time.clock) as gh:
        yield gh


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API_URL, assert_all_called=True) as router:
        yield router


def issue_json(number: int, **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "number": number,
        "title": f"Issue {number}",
        "body": "Steps to reproduce",
        "state": "open",
        "labels": [{"name": "good first issue"}],
        "assignees": [],
        "user": {"login": "reporter"},
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        "repository_url": f"{API_URL}/repos/{REPO}",
        "comments": 0,
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-10T00:00:00Z",
    }
    data.update(overrides)
    return data


def test_sends_api_headers(client: GitHubClient, api: respx.MockRouter) -> None:
    route = api.get(f"/repos/{REPO}").respond(
        json={
            "full_name": REPO,
            "html_url": f"https://github.com/{REPO}",
            "private": False,
            "archived": False,
            "fork": False,
            "language": "Python",
            "license": {"spdx_id": "MIT"},
            "default_branch": "main",
            "pushed_at": "2026-11-15T10:00:00Z",
        }
    )
    repo = client.get_repository(REPO)
    request = route.calls.last.request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert request.headers["Accept"] == "application/vnd.github+json"
    assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
    assert repo.license_spdx_id == "MIT"
    assert repo.language == "Python"
    assert repo.default_branch == "main"
    assert repo.pushed_at is not None


def test_no_token_sends_no_authorization(api: respx.MockRouter) -> None:
    route = api.get(f"/repos/{REPO}/issues/1").respond(json=issue_json(1))
    with GitHubClient() as gh:
        gh.get_issue(REPO, 1)
    assert "Authorization" not in route.calls.last.request.headers


def test_search_issues_builds_query_and_skips_pull_requests(
    client: GitHubClient, api: respx.MockRouter
) -> None:
    route = api.get("/search/issues").respond(
        json={
            "total_count": 2,
            "incomplete_results": False,
            "items": [issue_json(1), issue_json(2, pull_request={"url": "x"})],
        }
    )
    issues = client.search_issues("good first issue", "python", limit=10)
    params = route.calls.last.request.url.params
    assert params["q"] == (
        'is:issue is:open no:assignee archived:false label:"good first issue" language:python'
    )
    assert params["sort"] == "updated"
    assert params["per_page"] == "10"
    assert [i.number for i in issues] == [1]
    assert issues[0].repository == REPO
    assert issues[0].labels == ["good first issue"]


def test_pagination_follows_link_header(client: GitHubClient, api: respx.MockRouter) -> None:
    next_url = f"{API_URL}/search/issues?q=x&page=2&per_page=2"
    api.get("/search/issues", params={"page": "2"}).respond(
        json={"total_count": 3, "incomplete_results": False, "items": [issue_json(3)]}
    )
    api.get("/search/issues", params={"sort": "updated"}).respond(
        json={"total_count": 3, "incomplete_results": False, "items": [issue_json(1), issue_json(2)]},
        headers={"Link": f'<{next_url}>; rel="next", <{next_url}>; rel="last"'},
    )
    issues = client.search_issues("help wanted", "java", limit=50)
    assert [i.number for i in issues] == [1, 2, 3]


def test_pagination_stops_at_limit(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/commits").respond(
        json=[{"sha": str(n), "author": None, "commit": {"committer": {"date": None}}} for n in range(5)],
        headers={"Link": f'<{API_URL}/repos/{REPO}/commits?page=2>; rel="next"'},
    )
    assert len(client.list_commits(REPO, limit=3)) == 3


def test_get_issue(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/issues/5").respond(
        json=issue_json(5, assignees=[{"login": "someone"}], body=None)
    )
    issue = client.get_issue(REPO, 5)
    assert issue.assignees == ["someone"]
    assert issue.body == ""


def test_get_issue_rejects_pull_request(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/issues/5").respond(json=issue_json(5, pull_request={"url": "x"}))
    with pytest.raises(GitHubError, match="pull request"):
        client.get_issue(REPO, 5)


def test_get_file_decodes_content(client: GitHubClient, api: respx.MockRouter) -> None:
    content = base64.b64encode(b"# Contributing\n").decode()
    route = api.get(f"/repos/{REPO}/contents/docs/CONTRIBUTING.md").respond(
        json={
            "type": "file",
            "path": "docs/CONTRIBUTING.md",
            "sha": "abc",
            "encoding": "base64",
            "content": content,
        }
    )
    file = client.get_file(REPO, "docs/CONTRIBUTING.md", ref="main")
    assert file is not None
    assert file.text == "# Contributing\n"
    assert route.calls.last.request.url.params["ref"] == "main"


def test_get_file_missing_returns_none(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/contents/CONTRIBUTING.md").respond(404, json={"message": "Not Found"})
    assert client.get_file(REPO, "CONTRIBUTING.md") is None


def test_get_file_on_directory_returns_none(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/contents/docs").respond(json=[])
    assert client.get_file(REPO, "docs") is None


def test_list_directory(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/contents").respond(
        json=[
            {"name": "README.md", "path": "README.md", "type": "file"},
            {"name": ".github", "path": ".github", "type": "dir"},
        ]
    )
    api.get(f"/repos/{REPO}/contents/.github").respond(404, json={"message": "Not Found"})
    assert [e.name for e in client.list_directory(REPO)] == ["README.md", ".github"]
    assert client.list_directory(REPO, ".github") == []


def test_list_commits(client: GitHubClient, api: respx.MockRouter) -> None:
    route = api.get(f"/repos/{REPO}/commits").respond(
        json=[
            {
                "sha": "a1",
                "author": {"login": "dev"},
                "commit": {"committer": {"date": "2026-11-14T00:00:00Z"}},
            }
        ]
    )
    [commit] = client.list_commits(REPO, branch="main")
    assert commit.sha == "a1"
    assert commit.author == "dev"
    assert route.calls.last.request.url.params["sha"] == "main"


def test_list_merged_pull_requests(client: GitHubClient, api: respx.MockRouter) -> None:
    def pr(number: int, merged_at: str | None) -> dict[str, Any]:
        return {
            "number": number,
            "state": "closed",
            "user": {"login": "outsider"},
            "author_association": "CONTRIBUTOR",
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "created_at": "2026-10-01T00:00:00Z",
            "merged_at": merged_at,
            "closed_at": "2026-10-02T00:00:00Z",
        }

    route = api.get(f"/repos/{REPO}/pulls").respond(json=[pr(1, "2026-10-02T00:00:00Z"), pr(2, None)])
    merged = client.list_merged_pull_requests(REPO)
    assert [p.number for p in merged] == [1]
    assert merged[0].author_association == "CONTRIBUTOR"
    params = route.calls.last.request.url.params
    assert (params["state"], params["sort"], params["direction"]) == ("closed", "updated", "desc")


def test_list_issue_comments(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}/issues/3/comments").respond(
        json=[
            {
                "id": 9,
                "user": {"login": "helper"},
                "author_association": "NONE",
                "body": "I'm working on this",
                "created_at": "2026-11-12T00:00:00Z",
            }
        ]
    )
    [comment] = client.list_issue_comments(REPO, 3)
    assert comment.author == "helper"
    assert comment.body == "I'm working on this"


def test_list_linked_pull_requests(client: GitHubClient, api: respx.MockRouter) -> None:
    def xref(number: int, state: str, pull: dict[str, Any] | None) -> dict[str, Any]:
        issue: dict[str, Any] = {
            "number": number,
            "state": state,
            "html_url": f"https://github.com/{REPO}/pull/{number}",
            "repository_url": f"{API_URL}/repos/{REPO}",
        }
        if pull is not None:
            issue["pull_request"] = pull
        return {"event": "cross-referenced", "source": {"type": "issue", "issue": issue}}

    api.get(f"/repos/{REPO}/issues/3/timeline").respond(
        json=[
            {"event": "labeled"},
            xref(10, "open", {"merged_at": None}),
            xref(11, "closed", {"merged_at": "2026-11-01T00:00:00Z"}),
            xref(12, "open", None),  # an issue, not a pull request
            xref(10, "open", {"merged_at": None}),  # duplicate reference
        ]
    )
    linked = client.list_linked_pull_requests(REPO, 3)
    assert [(p.number, p.state, p.merged) for p in linked] == [(10, "open", False), (11, "closed", True)]


def test_retry_after_is_respected(client: GitHubClient, api: respx.MockRouter, fake_time: FakeTime) -> None:
    api.get(f"/repos/{REPO}/issues/1").mock(
        side_effect=[
            httpx.Response(403, headers={"retry-after": "7"}, json={"message": "secondary rate limit"}),
            httpx.Response(200, json=issue_json(1)),
        ]
    )
    assert client.get_issue(REPO, 1).number == 1
    assert fake_time.sleeps == [7.0]


def test_waits_until_reset_when_remaining_is_zero(
    client: GitHubClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    reset = int(fake_time.now) + 30
    api.get(f"/repos/{REPO}/issues/1").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)},
                json={"message": "API rate limit exceeded"},
            ),
            httpx.Response(200, json=issue_json(1)),
        ]
    )
    client.get_issue(REPO, 1)
    assert fake_time.sleeps == [31.0]


def test_secondary_limit_without_headers_backs_off_exponentially(
    client: GitHubClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    api.get(f"/repos/{REPO}/issues/1").mock(
        side_effect=[
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(429, json={"message": "slow down"}),
            httpx.Response(200, json=issue_json(1)),
        ]
    )
    client.get_issue(REPO, 1)
    assert fake_time.sleeps == [60.0, 120.0]


def test_gives_up_after_max_retries(api: respx.MockRouter, fake_time: FakeTime) -> None:
    route = api.get(f"/repos/{REPO}/issues/1").respond(429, json={"message": "slow down"})
    with (
        GitHubClient(max_retries=2, sleep=fake_time.sleep, clock=fake_time.clock) as gh,
        pytest.raises(RateLimited),
    ):
        gh.get_issue(REPO, 1)
    assert route.call_count == 3


def test_refuses_waits_longer_than_maximum(api: respx.MockRouter, fake_time: FakeTime) -> None:
    api.get(f"/repos/{REPO}/issues/1").respond(403, headers={"retry-after": "3600"}, json={})
    with (
        GitHubClient(max_wait_seconds=60, sleep=fake_time.sleep, clock=fake_time.clock) as gh,
        pytest.raises(RateLimited, match="longer than"),
    ):
        gh.get_issue(REPO, 1)
    assert fake_time.sleeps == []


def test_exhausted_quota_delays_the_next_request(
    client: GitHubClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    reset = int(fake_time.now) + 10
    api.get(f"/repos/{REPO}/issues/1").respond(
        json=issue_json(1), headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(reset)}
    )
    api.get(f"/repos/{REPO}/issues/2").respond(json=issue_json(2))
    client.get_issue(REPO, 1)
    assert fake_time.sleeps == []
    client.get_issue(REPO, 2)
    assert fake_time.sleeps == [11.0]


def test_plain_forbidden_is_not_retried(client: GitHubClient, api: respx.MockRouter) -> None:
    route = api.get(f"/repos/{REPO}/issues/1").respond(403, json={"message": "Resource not accessible"})
    with pytest.raises(GitHubError, match="403 Resource not accessible"):
        client.get_issue(REPO, 1)
    assert route.call_count == 1


def test_not_found(client: GitHubClient, api: respx.MockRouter) -> None:
    api.get(f"/repos/{REPO}").respond(404, json={"message": "Not Found"})
    with pytest.raises(NotFound):
        client.get_repository(REPO)


def test_timeout_is_retried_then_raised(
    client: GitHubClient, api: respx.MockRouter, fake_time: FakeTime
) -> None:
    route = api.get(f"/repos/{REPO}/issues/1").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(GitHubError, match="timed out"):
        client.get_issue(REPO, 1)
    assert route.call_count == 4
    assert fake_time.sleeps == [1.0, 2.0, 4.0]


def test_rejects_invalid_repository_names(client: GitHubClient) -> None:
    with pytest.raises(ValueError, match="owner/name"):
        client.get_repository("../../user")


def test_client_has_no_write_methods() -> None:
    public = {name for name in dir(GitHubClient) if not name.startswith("_")}
    assert public <= {
        "close",
        "search_issues",
        "get_repository",
        "get_issue",
        "get_file",
        "list_directory",
        "list_commits",
        "list_merged_pull_requests",
        "list_issue_comments",
        "list_linked_pull_requests",
    }
