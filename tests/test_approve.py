import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.approve import ApprovalError, approve, clean, reject, render_diff, render_summary
from mergeworthy.ledger import AttemptState, Ledger, LedgerError

runner = CliRunner()

DIFF = (
    "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-old\n+new\n"
    "diff --git a/tests/test_app.py b/tests/test_app.py\nnew file mode 100644\n--- /dev/null\n"
    "+++ b/tests/test_app.py\n@@ -0,0 +1 @@\n+def test(): assert True\n"
)
S = AttemptState


def waiting_attempt(
    ledger: Ledger, number: int = 12, diff: str = DIFF, title: str = "Crash on empty input"
) -> int:
    repo = ledger.upsert_repository("owner/repo", language="python")
    issue = ledger.upsert_issue(
        repo, number, title, ["good first issue"], f"https://github.com/owner/repo/issues/{number}"
    )
    ledger.set_issue_eligibility(
        issue,
        True,
        [{"name": "ai_policy", "status": "pass", "reason": "no policy against AI-assisted contributions"}],
    )
    attempt = ledger.create_attempt(issue)
    for state in (S.ELIGIBLE, S.WORKING):
        ledger.transition(attempt, state, f"to {state}")
    ledger.update_attempt(
        attempt,
        diff=diff,
        explanation="Return an empty result for empty input; added a regression test.",
        diff_stats={
            "files": ["src/app.py", "tests/test_app.py"],
            "test_files": ["tests/test_app.py"],
            "lines_changed_excluding_tests": 2,
            "test_lines_changed": 1,
        },
        test_results={"baseline": {"exit_code": 0, "timed_out": False, "seconds": 12.0}},
        commands=[{"command": "python -m pytest -q", "exit_code": 0, "timed_out": False}],
        verification=[
            {"rule": "tests_pass", "passed": True, "reason": "tests pass with the change"},
            {
                "rule": "untracked_build_output",
                "passed": True,
                "reason": "1 new untracked files under build-output folders, NOT in the diff: build/x.py",
                "details": {"informational": True, "files": ["build/x.py"]},
            },
        ],
        review_verdict="accept",
        review_reasons={
            "rubric_version": "1",
            "answers": {"solves_issue": {"answer": "yes", "reason": "handles the empty case"}},
            "reasons": [],
        },
        spend_usd=0.84,
    )
    ledger.transition(attempt, S.SELF_REVIEWED, "verification passed")
    ledger.transition(attempt, S.AWAITING_APPROVAL, "accepted by self-review")
    return attempt


@pytest.fixture
def env(tmp_path: Path) -> tuple[str, Path]:
    ledger_path = tmp_path / "ledger.sqlite3"
    config = tmp_path / "mergeworthy.toml"
    config.write_text(f'ledger_path = "{ledger_path.as_posix()}"\n', encoding="utf-8")
    return str(config), ledger_path


def invoke(config: str, args: list[str], answers: str) -> object:
    return runner.invoke(cli.app, ["review", *args, "-c", config], input=answers)


def state(ledger_path: Path, attempt: int) -> AttemptState:
    with Ledger(ledger_path) as ledger:
        return ledger.get_attempt(attempt).state


# --- CLI ----------------------------------------------------------------------------------------------


def test_nothing_waiting(env: tuple[str, Path]) -> None:
    result = invoke(env[0], [], "")
    assert result.exit_code == 0  # type: ignore[attr-defined]
    assert "no attempts are waiting" in result.output  # type: ignore[attr-defined]


def test_lists_waiting_attempts_and_quits(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [], "\n")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    assert "owner/repo" in out and "Crash on" in out and "$0.84" in out
    assert state(env[1], attempt) is S.AWAITING_APPROVAL


def test_approve_shows_everything_and_pins_the_diff(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [], f"{attempt}\na\nRead the diff and tests; correct and minimal\n")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    out = result.output  # type: ignore[attr-defined]
    for expected in (
        "Title:  Crash on empty input",  # issue
        "Return an empty result for empty input",  # summary
        "+def test(): assert True",  # diff
        "baseline: exit 0, 12s",  # tests
        "agent ran: python -m pytest -q -> exit 0",
        "[PASS] tests_pass",  # verification
        "[NOTE] untracked_build_output: 1 new untracked files under build-output folders, NOT in the diff",
        "Self-review: accept (rubric v1)",  # review
        "[yes] solves_issue: handles the empty case",
        "ai_policy: no policy against AI-assisted contributions",  # policy checks
        "$0.84",  # spend
        f"attempt {attempt} approved",
    ):
        assert expected in out, expected
    with Ledger(env[1]) as ledger:
        details = ledger.attempt_details(attempt)
        assert details["state"] == "approved"
        assert details["approved_diff_sha256"] == hashlib.sha256(DIFF.encode()).hexdigest()
        assert details["approved_files"] == ["src/app.py", "tests/test_app.py"]
        last = ledger.transitions(attempt)[-1]
        assert last.to_state is S.APPROVED
        assert "approved by a human: Read the diff and tests; correct and minimal" in last.reason


def test_reject_with_reason(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [str(attempt), "--no-pager"], "r\nTest does not cover the reported case\n")
    assert result.exit_code == 0, result.output  # type: ignore[attr-defined]
    with Ledger(env[1]) as ledger:
        assert ledger.get_attempt(attempt).state is S.HUMAN_REJECTED
        assert (
            ledger.transitions(attempt)[-1].reason
            == "rejected by a human: Test does not cover the reported case"
        )


@pytest.mark.parametrize("action", ["r", "a"])
def test_decision_without_reason_is_refused(env: tuple[str, Path], action: str) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [str(attempt), "--no-pager"], f"{action}\n   \n")
    assert result.exit_code == 1
    assert "a reason is required; nothing recorded" in result.output  # type: ignore[attr-defined]
    assert state(env[1], attempt) is S.AWAITING_APPROVAL


def test_skip_records_nothing(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [str(attempt), "--no-pager"], "s\n")
    assert result.exit_code == 0
    assert state(env[1], attempt) is S.AWAITING_APPROVAL


def test_unknown_answer_records_nothing(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
    result = invoke(env[0], [str(attempt), "--no-pager"], "yes please\n")
    assert result.exit_code == 2
    assert state(env[1], attempt) is S.AWAITING_APPROVAL


def test_attempt_not_waiting(env: tuple[str, Path]) -> None:
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger)
        ledger.transition(attempt, S.HUMAN_REJECTED, "no")
    result = invoke(env[0], [str(attempt)], "")
    assert result.exit_code == 1
    assert "not waiting for approval" in result.output  # type: ignore[attr-defined]


def test_untrusted_text_is_shown_without_escape_sequences(env: tuple[str, Path]) -> None:
    hostile = DIFF.replace("+new\n", "+new \x1b]8;;http://evil\x07link\x1b]8;;\x07 \x1b[2J\n")
    with Ledger(env[1]) as ledger:
        attempt = waiting_attempt(ledger, diff=hostile, title="Bug \x1b[31mred\x1b[0m title")
    result = invoke(env[0], [str(attempt), "--no-pager"], "s\n")
    out = result.output  # type: ignore[attr-defined]
    assert "\x1b" not in out
    assert "Bug red title" in out
    assert "+new link " in out


# --- functions --------------------------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    led = Ledger(tmp_path / "l.sqlite3")
    yield led  # type: ignore[misc]
    led.close()


def test_approve_requires_the_diff_that_was_shown(ledger: Ledger) -> None:
    attempt = waiting_attempt(ledger)
    with pytest.raises(ApprovalError, match="changed since it was shown"):
        approve(ledger, attempt, "ok", render_diff(ledger, attempt) + "+sneaky\n")
    assert ledger.get_attempt(attempt).state is S.AWAITING_APPROVAL


def test_approve_refuses_unsafe_diff(ledger: Ledger) -> None:
    bad = (
        "diff --git a/.git/config b/.git/config\n--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-a\n+b\n"
    )
    attempt = waiting_attempt(ledger, diff=bad)
    with pytest.raises(ApprovalError, match="not a safe patch"):
        approve(ledger, attempt, "ok", render_diff(ledger, attempt))


def test_approve_and_reject_need_reason_and_waiting_state(ledger: Ledger) -> None:
    attempt = waiting_attempt(ledger)
    with pytest.raises(ApprovalError, match="reason"):
        approve(ledger, attempt, " ", render_diff(ledger, attempt))
    with pytest.raises(ApprovalError, match="reason"):
        reject(ledger, attempt, "")
    reject(ledger, attempt, "not convinced")
    with pytest.raises(ApprovalError, match="not awaiting approval"):
        approve(ledger, attempt, "changed my mind", render_diff(ledger, attempt))


def test_record_approval_validates(ledger: Ledger) -> None:
    attempt = waiting_attempt(ledger)
    with pytest.raises(LedgerError, match="SHA-256"):
        ledger.record_approval(attempt, "abc", ["a"], "ok")
    with pytest.raises(LedgerError, match="reason"):
        ledger.record_approval(attempt, "0" * 64, ["a"], "")


def test_summary_mentions_unverified_agent_claims(ledger: Ledger) -> None:
    attempt = waiting_attempt(ledger)
    assert "Summary from the coding agent (unverified claims)" in render_summary(ledger, attempt)


def test_clean_keeps_newlines_and_tabs() -> None:
    assert clean("a\tb\nc\x1b[1md\x07") == "a\tb\ncd"
