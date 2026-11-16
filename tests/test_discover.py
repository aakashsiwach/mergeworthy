from collections.abc import Iterator
from pathlib import Path

import pytest
from fakes import NOW, FakeGitHub, load_fixture, make_issue
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.config import Config, parse_config
from mergeworthy.discover import check_and_record, discover, parse_target, record_decision
from mergeworthy.eligibility import Status
from mergeworthy.github import LinkedPullRequest
from mergeworthy.ledger import AttemptState, Decision, Ledger


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


def github_with_issues() -> FakeGitHub:
    welcoming = load_fixture("welcoming", full_name="good/repo")
    welcoming.issues[1] = make_issue("good/repo", 1)
    welcoming.issues[2] = make_issue("good/repo", 2, assignees=["someone"])
    welcoming.issues[3] = make_issue("good/repo", 3)
    welcoming.linked[3] = [
        LinkedPullRequest(
            repository="good/repo", number=9, state="open", merged=False, html_url="https://p/9"
        )
    ]
    forbidden = load_fixture("ai_forbidden", full_name="noai/repo")
    forbidden.issues[1] = make_issue("noai/repo", 1)
    unclear = load_fixture("ai_unclear", full_name="unclear/repo")
    unclear.issues[1] = make_issue("unclear/repo", 1)
    java = load_fixture("welcoming", full_name="java/repo", language="Java")
    gh = FakeGitHub(welcoming, forbidden, unclear, java)
    gh.searches[("good first issue", "python")] = [make_issue("good/repo", 1), make_issue("noai/repo", 1)]
    gh.searches[("help wanted", "python")] = [make_issue("good/repo", 1), make_issue("denied/repo", 4)]
    gh.searches[("good first issue", "java")] = [make_issue("java/repo", 5)]
    return gh


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("owner/repo", ("owner/repo", None)),
        ("owner/repo#12", ("owner/repo", 12)),
        ("https://github.com/owner/repo", ("owner/repo", None)),
        ("https://github.com/owner/repo.git", ("owner/repo", None)),
        ("https://github.com/owner/repo/issues/12", ("owner/repo", 12)),
        ("https://github.com/owner/repo/issues/12#issuecomment-1", ("owner/repo", 12)),
    ],
)
def test_parse_target(target: str, expected: tuple[str, int | None]) -> None:
    assert parse_target(target) == expected


@pytest.mark.parametrize("target", ["owner", "https://gitlab.com/a/b", "a/b/c", "a/b#x"])
def test_parse_target_rejects(target: str) -> None:
    with pytest.raises(ValueError):
        parse_target(target)


def test_discover_stores_candidates_once(ledger: Ledger) -> None:
    gh = github_with_issues()
    config = parse_config({"deny_list": ["denied/repo"]})
    found = discover(gh, ledger, config)
    refs = [(d.issue.repository, d.issue.number) for d in found]
    assert refs == [("java/repo", 5), ("good/repo", 1), ("noai/repo", 1)]
    assert all(d.new for d in found)
    assert ledger.get_repository("denied/repo") is None
    assert len(ledger.attempts_in_state(AttemptState.DISCOVERED)) == 3

    again = discover(gh, ledger, config)
    assert not any(d.new for d in again)
    assert len(ledger.attempts_in_state(AttemptState.DISCOVERED)) == 3


def test_discover_respects_language_and_limit(ledger: Ledger) -> None:
    gh = github_with_issues()
    found = discover(gh, ledger, Config(), languages=["python"], limit=1)
    assert [(d.issue.repository, d.language) for d in found] == [("good/repo", "python")]
    assert not any("java" in call for call in gh.calls)


def test_discover_skips_ledger_deny_listed(ledger: Ledger) -> None:
    ledger.upsert_repository("noai/repo", deny_listed=True)
    found = discover(github_with_issues(), ledger, Config(), languages=["python"])
    assert "noai/repo" not in {d.issue.repository for d in found}


def test_discover_rejects_unconfigured_language(ledger: Ledger) -> None:
    with pytest.raises(ValueError, match="not enabled"):
        discover(github_with_issues(), ledger, parse_config({"languages": ["python"]}), languages=["java"])


def test_check_eligible_issue_moves_attempt_to_eligible(ledger: Ledger) -> None:
    gh = github_with_issues()
    discover(gh, ledger, Config(), languages=["python"])
    outcome = check_and_record(gh, ledger, Config(), "good/repo#1", now=NOW)
    assert outcome.eligible
    assert outcome.attempt_state is AttemptState.ELIGIBLE
    repo = ledger.get_repository("good/repo")
    assert repo is not None
    assert repo["license"] == "MIT"
    assert repo["policy_check"]["ai_policy"]["status"] == "pass"


@pytest.mark.parametrize(
    ("target", "reason"),
    [
        ("good/repo#2", "unassigned"),
        ("good/repo#3", "no_open_pull_request"),
        ("noai/repo#1", "ai_policy"),
    ],
)
def test_check_ineligible_issue_is_skipped_with_reason(ledger: Ledger, target: str, reason: str) -> None:
    gh = github_with_issues()
    outcome = check_and_record(gh, ledger, Config(), target, now=NOW)
    assert not outcome.eligible
    assert outcome.attempt_state is AttemptState.SKIPPED
    assert outcome.attempt_id is not None
    last = ledger.transitions(outcome.attempt_id)[-1]
    assert reason in last.reason
    assert not last.reason.startswith("needs human")


def test_check_unclear_policy_waits_for_a_human(ledger: Ledger) -> None:
    gh = github_with_issues()
    outcome = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    assert outcome.needs_human
    assert outcome.attempt_state is AttemptState.NEEDS_HUMAN
    repo = ledger.get_repository("unclear/repo")
    assert repo is not None
    assert repo["policy_check"]["needs_human"] is True
    [question] = ledger.pending_decisions()
    assert (question.subject, question.check_name) == ("unclear/repo", "ai_policy")

    # Checking again without a decision changes nothing and asks nothing new.
    again = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    assert again.attempt_state is AttemptState.NEEDS_HUMAN
    assert len(ledger.pending_decisions()) == 1


def test_allow_decision_is_reused_until_policy_changes(ledger: Ledger) -> None:
    gh = github_with_issues()
    first = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    record_decision(ledger, "unclear/repo", "ai_policy", Decision.ALLOW, "LLM note is harmless")

    after = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    assert after.eligible
    assert after.attempt_id == first.attempt_id
    assert after.attempt_state is AttemptState.ELIGIBLE
    result = after.repository.get("ai_policy")
    assert result is not None
    assert result.status is Status.PASS
    assert "a human allowed this" in result.reason

    # The policy files change: the old decision no longer applies and a new question is asked.
    gh.repos["unclear/repo"].files["docs/CONTRIBUTING.md"] += "\nWe are thinking about AI tools.\n"
    changed = check_and_record(gh, ledger, Config(), "unclear/repo", now=NOW)
    assert changed.needs_human
    assert len(ledger.pending_decisions()) == 1


def test_deny_decision_skips_waiting_attempts(ledger: Ledger) -> None:
    gh = github_with_issues()
    first = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    outcome = record_decision(ledger, "unclear/repo", "ai_policy", Decision.DENY, "reads as a refusal")
    assert outcome.skipped_attempts == [first.attempt_id]
    assert first.attempt_id is not None
    assert ledger.get_attempt(first.attempt_id).state is AttemptState.SKIPPED

    later = check_and_record(gh, ledger, Config(), "unclear/repo", now=NOW)
    assert later.blocked
    assert not later.needs_human


def test_hard_failure_wins_over_unclear(ledger: Ledger) -> None:
    gh = github_with_issues()
    gh.repos["unclear/repo"].issues[1] = make_issue("unclear/repo", 1, assignees=["someone"])
    outcome = check_and_record(gh, ledger, Config(), "unclear/repo#1", now=NOW)
    assert outcome.blocked
    assert outcome.attempt_state is AttemptState.SKIPPED


def test_vague_issue_waits_for_a_human(ledger: Ledger) -> None:
    gh = github_with_issues()
    body = "It would be nice to have a nicer way of handling configuration in the main module overall."
    gh.repos["good/repo"].issues[1] = make_issue("good/repo", 1, body=body)
    outcome = check_and_record(gh, ledger, Config(), "good/repo#1", now=NOW)
    assert outcome.attempt_state is AttemptState.NEEDS_HUMAN
    record_decision(ledger, "good/repo#1", "specific", Decision.ALLOW, "clear enough")
    assert check_and_record(gh, ledger, Config(), "good/repo#1", now=NOW).eligible


def test_check_repository_only_records_no_attempt(ledger: Ledger) -> None:
    outcome = check_and_record(github_with_issues(), ledger, Config(), "good/repo", now=NOW)
    assert outcome.issue is None
    assert outcome.attempt_id is None
    assert ledger.get_repository("good/repo") is not None


def test_check_does_not_move_attempts_past_discovered(ledger: Ledger) -> None:
    gh = github_with_issues()
    first = check_and_record(gh, ledger, Config(), "good/repo#1", now=NOW)
    assert first.attempt_id is not None
    ledger.transition(first.attempt_id, AttemptState.WORKING, "started")
    again = check_and_record(gh, ledger, Config(), "good/repo#1", now=NOW)
    assert again.attempt_id == first.attempt_id
    assert again.attempt_state is AttemptState.WORKING


# --- CLI --------------------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config = tmp_path / "mergeworthy.toml"
    ledger_path = (tmp_path / "ledger.sqlite3").as_posix()
    config.write_text(f'ledger_path = "{ledger_path}"\n', encoding="utf-8")
    gh = github_with_issues()
    monkeypatch.setattr(cli, "make_client", lambda: gh)
    return config


def test_cli_discover(cli_env: Path) -> None:
    result = runner.invoke(cli.app, ["discover", "--language", "python", "--config", str(cli_env)])
    assert result.exit_code == 0, result.output
    assert "good/repo#1" in result.output
    assert "3 candidates, 3 new" in result.output


def test_cli_check_exit_codes(cli_env: Path) -> None:
    ok = runner.invoke(cli.app, ["check", "good/repo#1", "-c", str(cli_env)])
    assert ok.exit_code == 0, ok.output
    assert "eligible" in ok.output

    no = runner.invoke(cli.app, ["check", "https://github.com/noai/repo/issues/1", "-c", str(cli_env)])
    assert no.exit_code == 1, no.output
    assert "forbids or discourages" in no.output

    human = runner.invoke(cli.app, ["check", "unclear/repo", "-c", str(cli_env)])
    assert human.exit_code == 3, human.output
    assert "needs a human decision" in human.output


def test_cli_check_bad_target(cli_env: Path) -> None:
    result = runner.invoke(cli.app, ["check", "not a target", "-c", str(cli_env)])
    assert result.exit_code == 1
    assert "expected owner/repo" in result.output


def test_cli_refuses_config_with_secret(tmp_path: Path) -> None:
    config = tmp_path / "mergeworthy.toml"
    config.write_text('github_token = "FAKE_TOKEN_FOR_TESTS"\n', encoding="utf-8")
    result = runner.invoke(cli.app, ["check", "good/repo", "-c", str(config)])
    assert result.exit_code == 2
    assert "FAKE_TOKEN_FOR_TESTS" not in result.output


def test_safe_strips_terminal_escapes() -> None:
    assert cli.safe("title\x1b[31m red\x07").plain == "title red"


def test_cli_decide_lists_and_answers(cli_env: Path) -> None:
    runner.invoke(cli.app, ["check", "unclear/repo#1", "-c", str(cli_env)])
    listed = runner.invoke(cli.app, ["decide", "-c", str(cli_env)])
    assert listed.exit_code == 0, listed.output
    assert "unclear/repo" in listed.output
    assert "1 open questions" in listed.output

    no_reason = runner.invoke(cli.app, ["decide", "unclear/repo", "ai_policy", "--allow", "-c", str(cli_env)])
    assert no_reason.exit_code == 2
    both = runner.invoke(
        cli.app, ["decide", "unclear/repo", "ai_policy", "--allow", "--deny", "-r", "x", "-c", str(cli_env)]
    )
    assert both.exit_code == 2

    done = runner.invoke(
        cli.app,
        ["decide", "unclear/repo", "ai_policy", "--deny", "-r", "unclear wording", "-c", str(cli_env)],
    )
    assert done.exit_code == 0, done.output
    assert "skipped attempts: 1" in done.output
    unknown = runner.invoke(
        cli.app, ["decide", "unclear/repo", "ai_policy", "--deny", "-r", "again", "-c", str(cli_env)]
    )
    assert unknown.exit_code == 1
    assert "no open question" in unknown.output
