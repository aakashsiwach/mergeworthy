from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_submit import UPSTREAM, approved_attempt
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.config import Config, parse_config
from mergeworthy.followup import asks_to_close, ci_status, follow_up, says_ai_unwelcome
from mergeworthy.github import CheckRun, Comment, PullRequestStatus, Review
from mergeworthy.ledger import AttemptState, Ledger

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
S = AttemptState


class FakePulls:
    def __init__(self) -> None:
        self.status = PullRequestStatus(
            number=77,
            state="open",
            merged=False,
            merged_at=None,
            closed_at=None,
            head_sha="abc1234",
            html_url=f"https://github.com/{UPSTREAM}/pull/77",
        )
        self.runs: list[CheckRun] = []
        self.comments: list[Comment] = []
        self.reviews: list[Review] = []

    def get_pull_request(self, full_name: str, number: int) -> PullRequestStatus:
        assert (full_name, number) == (UPSTREAM, 77)
        return self.status

    def list_check_runs(self, full_name: str, ref: str) -> list[CheckRun]:
        return self.runs

    def list_issue_comments(self, full_name: str, number: int, *, limit: int = 300) -> list[Comment]:
        return self.comments

    def list_reviews(self, full_name: str, number: int) -> list[Review]:
        return self.reviews


def comment(cid: int, body: str, association: str = "MEMBER", author: str = "maintainer") -> Comment:
    return Comment(id=cid, author=author, author_association=association, body=body, created_at=NOW)


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


def submitted(ledger: Ledger) -> int:
    attempt = approved_attempt(ledger)
    ledger.record_submission(
        attempt,
        repository=UPSTREAM,
        number=77,
        url=f"https://github.com/{UPSTREAM}/pull/77",
        head_repository="me/repo",
        branch="mergeworthy/issue-12-1",
        reason="opened",
    )
    return attempt


def run(ledger: Ledger, gh: FakePulls, config: Config | None = None) -> list:  # type: ignore[type-arg]
    return follow_up(gh, ledger, config or Config(), now=NOW)


def test_open_pull_request_moves_to_in_review_and_records_ci(ledger: Ledger) -> None:
    attempt = submitted(ledger)
    gh = FakePulls()
    gh.runs = [CheckRun(name="test", status="in_progress", conclusion=None)]
    [result] = run(ledger, gh)
    assert result.ci_status == "pending"
    assert ledger.get_attempt(attempt).state is S.IN_REVIEW
    assert ledger.pull_requests()[0]["ci_status"] == "pending"
    run(ledger, gh)  # idempotent
    assert ledger.get_attempt(attempt).state is S.IN_REVIEW
    assert ledger.drafts() == []


def test_merged(ledger: Ledger) -> None:
    attempt = submitted(ledger)
    gh = FakePulls()
    gh.status = gh.status.model_copy(update={"state": "closed", "merged": True, "merged_at": NOW})
    [result] = run(ledger, gh)
    assert result.events == ["merged"]
    assert ledger.get_attempt(attempt).state is S.MERGED
    states = [t.to_state for t in ledger.transitions(attempt)][-2:]
    assert states == [S.IN_REVIEW, S.MERGED]
    pr = ledger.pull_requests()[0]
    assert (pr["state"], pr["merged_at"]) == ("merged", NOW.isoformat())
    assert run(ledger, gh) == []  # no longer open in the ledger


def test_closed_without_merge_starts_cooldown_and_keeps_the_reason(ledger: Ledger) -> None:
    attempt = submitted(ledger)
    gh = FakePulls()
    gh.status = gh.status.model_copy(update={"state": "closed", "closed_at": NOW})
    gh.comments = [comment(1, "Thanks, but we fixed this differently in #80.")]
    [result] = run(ledger, gh)
    assert ledger.get_attempt(attempt).state is S.CLOSED
    pr = ledger.pull_requests()[0]
    assert pr["state"] == "closed"
    assert pr["outcome_notes"] == "Thanks, but we fixed this differently in #80."
    repo = ledger.get_repository(UPSTREAM)
    assert repo is not None
    assert repo["cooldown_until"] == (NOW + timedelta(days=60)).isoformat(timespec="seconds")
    assert repo["deny_listed"] is False
    assert "cooldown until 2027-01-30" in result.events[-1]


@pytest.mark.parametrize("pr_state", ["open", "closed"])
def test_ai_unwelcome_adds_repository_to_deny_list(ledger: Ledger, pr_state: str) -> None:
    attempt = submitted(ledger)
    gh = FakePulls()
    gh.status = gh.status.model_copy(
        update={"state": pr_state, "closed_at": NOW if pr_state == "closed" else None}
    )
    gh.comments = [comment(5, "AI contributions are not welcome in this project. Closing.")]
    [result] = run(ledger, gh)
    repo = ledger.get_repository(UPSTREAM)
    assert repo is not None and repo["deny_listed"] is True
    assert any("deny list" in e for e in result.events)
    if pr_state == "open":
        [draft] = ledger.drafts()
        assert draft["kind"] == "close"
        assert draft["body"] == "Thanks for taking a look. I'll close this."
        assert ledger.get_attempt(attempt).state is S.IN_REVIEW  # nothing is closed without approval
    else:
        assert ledger.get_attempt(attempt).state is S.CLOSED


def test_maintainer_feedback_becomes_reply_drafts_once(ledger: Ledger) -> None:
    submitted(ledger)
    gh = FakePulls()
    gh.comments = [
        comment(1, "Could you add a test for None as well?"),
        comment(2, "+1, I hit this too", association="NONE", author="user"),  # not a maintainer
        comment(3, "Updated.", author="me"),  # our own comment
    ]
    gh.reviews = [
        Review(id=9, author="lead", author_association="OWNER", state="CHANGES_REQUESTED", body="Rename x.",
               submitted_at=NOW)
    ]  # fmt: skip
    run(ledger, gh, parse_config({"github_login": "me"}))
    drafts = ledger.drafts()
    assert [(d["kind"], d["source"]) for d in drafts] == [("reply", "comment:1"), ("reply", "review:9")]
    assert drafts[0]["body"] is None  # a human writes the reply
    assert drafts[1]["context"]["body"] == "[CHANGES_REQUESTED] Rename x."
    run(ledger, gh, parse_config({"github_login": "me"}))
    assert len(ledger.drafts()) == 2


def test_close_request_without_ai_complaint(ledger: Ledger) -> None:
    submitted(ledger)
    gh = FakePulls()
    gh.comments = [comment(4, "We won't merge this, please close this PR.")]
    run(ledger, gh)
    [draft] = ledger.drafts()
    assert draft["kind"] == "close"
    repo = ledger.get_repository(UPSTREAM)
    assert repo is not None and repo["deny_listed"] is False


def test_ci_failure_becomes_a_fix_draft(ledger: Ledger) -> None:
    submitted(ledger)
    gh = FakePulls()
    gh.runs = [
        CheckRun(name="lint", status="completed", conclusion="success"),
        CheckRun(name="test (3.12)", status="completed", conclusion="failure"),
    ]
    [result] = run(ledger, gh)
    assert result.ci_status == "failure"
    [draft] = ledger.drafts()
    assert (draft["kind"], draft["source"]) == ("fix", "ci:abc1234")
    assert draft["context"]["failing"] == ["test (3.12)"]


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([], "none"),
        ([("a", "completed", "success"), ("b", "completed", "skipped")], "success"),
        ([("a", "completed", "success"), ("b", "queued", None)], "pending"),
        ([("a", "completed", "timed_out"), ("b", "queued", None)], "failure"),
    ],
)
def test_ci_status(runs: list[tuple[str, str, str | None]], expected: str) -> None:
    assert ci_status([CheckRun(name=n, status=s, conclusion=c) for n, s, c in runs]) == expected


@pytest.mark.parametrize(
    ("text", "unwelcome"),
    [
        ("We do not accept AI-generated contributions.", True),
        ("This looks like AI slop.", True),
        ("Closing: LLM-generated PRs are spam to us.", True),
        ("Thanks! Could you add a test?", False),
        ("We use AI for triage; looks good.", False),
    ],
)
def test_says_ai_unwelcome(text: str, unwelcome: bool) -> None:
    assert says_ai_unwelcome(text) is unwelcome


def test_asks_to_close() -> None:
    assert asks_to_close("Please close this PR, it's a duplicate")
    assert asks_to_close("Closing in favour of #12")
    assert not asks_to_close("Looks close to done!")


def test_cli_followup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger_path = tmp_path / "ledger.sqlite3"
    with Ledger(ledger_path) as led:
        submitted(led)
    cfg = tmp_path / "mergeworthy.toml"
    cfg.write_text(f'ledger_path = "{ledger_path.as_posix()}"\n', encoding="utf-8")
    gh = FakePulls()
    gh.comments = [comment(1, "Could you add a test for None as well?")]
    monkeypatch.setattr(cli, "make_followup_client", lambda: gh)
    result = CliRunner().invoke(cli.app, ["followup", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "reply draft for comment:1" in result.output
    assert "1 drafts wait for a human; nothing was posted." in result.output
