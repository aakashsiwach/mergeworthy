from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import NOW, FakeGitHub, load_fixture, make_issue

from mergeworthy.config import Config, parse_config
from mergeworthy.discover import check_and_record, record_decision
from mergeworthy.eligibility import MentionKind, Status, check_issue, classify_mention, recheck_issue
from mergeworthy.github import LinkedPullRequest, PullRequestMention
from mergeworthy.ledger import AttemptState, Decision, Ledger
from mergeworthy.recheck import AwaitingHumanDecision, NoLongerAvailable, ensure_still_available

REPO = "good/repo"


def mention(body: str, title: str = "Some change", number: int = 8) -> PullRequestMention:
    return PullRequestMention(
        number=number, title=title, body=body, html_url=f"https://github.com/{REPO}/pull/{number}"
    )


@pytest.mark.parametrize(
    ("title", "body", "kind"),
    [
        ("Fix crash", "Fixes #12", MentionKind.CLOSES),
        ("Fix crash", "closes: #12", MentionKind.CLOSES),
        ("Fix crash", f"Resolves {REPO}#12", MentionKind.CLOSES),
        ("Fix crash", f"See https://github.com/{REPO}/issues/12 for details", MentionKind.CLOSES),
        ("Fix crash (#12)", "", MentionKind.MENTIONS),
        ("Refactor", "Related to #12, maybe.", MentionKind.MENTIONS),
        ("Refactor", "Part of issue 12", MentionKind.MENTIONS),
        ("Refactor", "Fixes #123", MentionKind.NONE),  # another issue
        ("Refactor", "Fixes other/repo#12", MentionKind.NONE),  # another repository
        ("Bump version to 1.12", "", MentionKind.NONE),
        ("Refactor", "See line 12 of parser.py", MentionKind.NONE),
    ],
)
def test_classify_mention(title: str, body: str, kind: MentionKind) -> None:
    assert classify_mention(mention(body, title), REPO, 12) is kind


def github() -> FakeGitHub:
    fake = load_fixture("welcoming", full_name=REPO)
    fake.issues[12] = make_issue(REPO, 12)
    return FakeGitHub(fake)


def no_pr_result(gh: FakeGitHub) -> tuple[Status, str]:
    report = check_issue(gh, Config(), gh.get_issue(REPO, 12), now=NOW)
    result = report.get("no_open_pull_request")
    assert result is not None
    return result.status, result.reason


def test_search_finds_closing_pull_request_missing_from_timeline() -> None:
    gh = github()
    gh.repos[REPO].mentions[12] = [mention("This fixes #12")]
    status, reason = no_pr_result(gh)
    assert status is Status.FAIL
    assert "/pull/8" in reason


def test_uncertain_mention_needs_human() -> None:
    gh = github()
    gh.repos[REPO].mentions[12] = [mention("Related to #12")]
    status, reason = no_pr_result(gh)
    assert status is Status.UNCLEAR
    assert "a human must decide" in reason


def test_unrelated_search_hits_pass() -> None:
    gh = github()
    gh.repos[REPO].mentions[12] = [mention("Bump to 1.12")]
    assert no_pr_result(gh)[0] is Status.PASS


def test_issue_assigned_to_own_login_counts_as_unassigned() -> None:
    gh = github()
    issue = make_issue(REPO, 12, assignees=["Me"])
    config = parse_config({"github_login": "@me"})
    result = check_issue(gh, config, issue, now=NOW).get("unassigned")
    assert result is not None
    assert result.status is Status.PASS
    other = check_issue(gh, Config(), issue, now=NOW).get("unassigned")
    assert other is not None
    assert other.status is Status.FAIL


def test_invalid_login_is_rejected() -> None:
    from mergeworthy.config import ConfigError

    with pytest.raises(ConfigError, match="login"):
        parse_config({"github_login": "not a login"})


def test_recheck_issue_runs_only_the_changing_checks() -> None:
    gh = github()
    report = recheck_issue(gh, Config(), gh.get_issue(REPO, 12))
    assert [r.name for r in report.results] == ["open", "unassigned", "no_open_pull_request"]
    assert report.eligible


# --- ensure_still_available -------------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


def eligible_attempt(gh: FakeGitHub, ledger: Ledger) -> int:
    outcome = check_and_record(gh, ledger, Config(), f"{REPO}#12", now=NOW)
    assert outcome.attempt_state is AttemptState.ELIGIBLE
    assert outcome.attempt_id is not None
    return outcome.attempt_id


def test_available_issue_passes(ledger: Ledger) -> None:
    gh = github()
    attempt = eligible_attempt(gh, ledger)
    assert ensure_still_available(gh, ledger, Config(), attempt).eligible
    assert ledger.get_attempt(attempt).state is AttemptState.ELIGIBLE


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("closed", "open: issue is closed"),
        ("assigned", "unassigned: assigned to someone"),
        ("linked", "no_open_pull_request: linked open pull request"),
    ],
)
def test_taken_issue_abandons_attempt(ledger: Ledger, change: str, reason: str) -> None:
    gh = github()
    attempt = eligible_attempt(gh, ledger)
    repo = gh.repos[REPO]
    if change == "closed":
        repo.issues[12] = make_issue(REPO, 12, state="closed")
    elif change == "assigned":
        repo.issues[12] = make_issue(REPO, 12, assignees=["someone"])
    else:
        repo.linked[12] = [
            LinkedPullRequest(repository=REPO, number=9, state="open", merged=False, html_url="https://p/9")
        ]
    with pytest.raises(NoLongerAvailable):
        ensure_still_available(gh, ledger, Config(), attempt)
    assert ledger.get_attempt(attempt).state is AttemptState.ABANDONED
    assert reason in ledger.transitions(attempt)[-1].reason


def test_uncertain_mention_blocks_without_state_change_until_decided(ledger: Ledger) -> None:
    gh = github()
    attempt = eligible_attempt(gh, ledger)
    ledger.transition(attempt, AttemptState.WORKING, "started")
    gh.repos[REPO].mentions[12] = [mention("Related to #12")]
    with pytest.raises(AwaitingHumanDecision):
        ensure_still_available(gh, ledger, Config(), attempt)
    assert ledger.get_attempt(attempt).state is AttemptState.WORKING
    [question] = ledger.pending_decisions()
    assert (question.subject, question.check_name) == (f"{REPO}#12", "no_open_pull_request")

    record_decision(ledger, f"{REPO}#12", "no_open_pull_request", Decision.ALLOW, "unrelated refactor")
    assert ensure_still_available(gh, ledger, Config(), attempt).eligible


def test_terminal_attempt_is_refused(ledger: Ledger) -> None:
    gh = github()
    attempt = eligible_attempt(gh, ledger)
    ledger.transition(attempt, AttemptState.SKIPPED, "changed our mind")
    with pytest.raises(NoLongerAvailable, match="already skipped"):
        ensure_still_available(gh, ledger, Config(), attempt)
