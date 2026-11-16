import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from mergeworthy.ledger import (
    ALLOWED_TRANSITIONS,
    MIGRATIONS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    AttemptState,
    Decision,
    InvalidTransition,
    Ledger,
    LedgerError,
    migrate,
)

S = AttemptState

_MAIN = [S.ELIGIBLE, S.WORKING, S.SELF_REVIEWED, S.AWAITING_APPROVAL, S.APPROVED, S.SUBMITTED, S.IN_REVIEW]
_CLAIM = [S.ELIGIBLE, S.CLAIM_DRAFTED, S.CLAIM_APPROVED, S.CLAIMED]

# A path from "discovered" to every non-terminal state, so each allowed edge can be exercised for real.
PATH_TO: dict[AttemptState, list[AttemptState]] = {S.DISCOVERED: [], S.NEEDS_HUMAN: [S.NEEDS_HUMAN]}
for _path in (_MAIN, _CLAIM):
    for _i, _state in enumerate(_path):
        PATH_TO.setdefault(_state, _path[: _i + 1])

ALL_EDGES = [(src, dst) for src, targets in ALLOWED_TRANSITIONS.items() for dst in sorted(targets)]


@pytest.fixture
def ledger(tmp_path: Path) -> Iterator[Ledger]:
    with Ledger(tmp_path / "ledger.sqlite3") as led:
        yield led


def new_attempt(ledger: Ledger, number: int = 1) -> int:
    repo = ledger.upsert_repository("owner/repo", language="python")
    issue = ledger.upsert_issue(repo, number, "Title", ["good first issue"], f"https://x/{number}")
    return ledger.create_attempt(issue)


def advance(ledger: Ledger, attempt_id: int, path: list[AttemptState]) -> None:
    for state in path:
        ledger.transition(attempt_id, state, f"to {state}")


def test_diagram_edges() -> None:
    A = {S.ABANDONED}
    assert ALLOWED_TRANSITIONS[S.DISCOVERED] == {S.ELIGIBLE, S.SKIPPED, S.NEEDS_HUMAN} | A
    assert ALLOWED_TRANSITIONS[S.NEEDS_HUMAN] == {S.ELIGIBLE, S.SKIPPED} | A
    assert ALLOWED_TRANSITIONS[S.ELIGIBLE] == {S.CLAIM_DRAFTED, S.WORKING, S.SKIPPED} | A
    assert ALLOWED_TRANSITIONS[S.CLAIM_DRAFTED] == {S.CLAIM_APPROVED, S.HUMAN_REJECTED} | A
    assert ALLOWED_TRANSITIONS[S.CLAIM_APPROVED] == {S.CLAIMED, S.DECLINED} | A
    assert ALLOWED_TRANSITIONS[S.CLAIMED] == {S.WORKING, S.DECLINED} | A
    assert ALLOWED_TRANSITIONS[S.WORKING] == {S.SELF_REVIEWED, S.FAILED} | A
    assert ALLOWED_TRANSITIONS[S.SELF_REVIEWED] == {S.AWAITING_APPROVAL, S.REJECTED} | A
    assert ALLOWED_TRANSITIONS[S.AWAITING_APPROVAL] == {S.APPROVED, S.HUMAN_REJECTED} | A
    assert ALLOWED_TRANSITIONS[S.APPROVED] == {S.SUBMITTED} | A
    assert ALLOWED_TRANSITIONS[S.SUBMITTED] == {S.IN_REVIEW, S.CLOSED} | A
    assert ALLOWED_TRANSITIONS[S.IN_REVIEW] == {S.MERGED, S.CLOSED} | A
    assert set(ALLOWED_TRANSITIONS) == set(AttemptState)
    assert TERMINAL_STATES == {
        S.SKIPPED, S.FAILED, S.REJECTED, S.HUMAN_REJECTED, S.MERGED, S.CLOSED, S.DECLINED, S.ABANDONED,
    }  # fmt: skip
    assert set(PATH_TO) == set(AttemptState) - TERMINAL_STATES


@pytest.mark.parametrize("state", sorted(set(AttemptState) - TERMINAL_STATES))
def test_every_non_terminal_state_can_be_abandoned(ledger: Ledger, state: AttemptState) -> None:
    attempt_id = new_attempt(ledger)
    advance(ledger, attempt_id, PATH_TO[state])
    assert ledger.transition(attempt_id, S.ABANDONED, "issue was closed").state is S.ABANDONED


@pytest.mark.parametrize("state", sorted(TERMINAL_STATES))
def test_terminal_states_cannot_be_abandoned(state: AttemptState) -> None:
    assert S.ABANDONED not in ALLOWED_TRANSITIONS[state]


@pytest.mark.parametrize(("source", "target"), ALL_EDGES, ids=str)
def test_every_allowed_transition(ledger: Ledger, source: AttemptState, target: AttemptState) -> None:
    attempt_id = new_attempt(ledger)
    advance(ledger, attempt_id, PATH_TO[source])
    attempt = ledger.transition(attempt_id, target, "because")
    assert attempt.state == target
    last = ledger.transitions(attempt_id)[-1]
    assert (last.from_state, last.to_state, last.reason) == (source, target, "because")


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ([], S.WORKING),  # must be checked for eligibility first
        ([], S.APPROVED),
        (PATH_TO[S.WORKING], S.SUBMITTED),  # cannot skip self-review and human approval
        (PATH_TO[S.SELF_REVIEWED], S.APPROVED),  # cannot skip human approval
        (PATH_TO[S.AWAITING_APPROVAL], S.SUBMITTED),
        (PATH_TO[S.APPROVED], S.MERGED),
        (PATH_TO[S.CLAIM_DRAFTED], S.WORKING),  # claim must be approved and made first
        (PATH_TO[S.CLAIM_DRAFTED], S.DECLINED),  # a maintainer can only decline an approved claim
        (PATH_TO[S.WORKING], S.DECLINED),
        (PATH_TO[S.APPROVED], S.CLOSED),  # not submitted yet
        (PATH_TO[S.ELIGIBLE], S.HUMAN_REJECTED),
        ([S.SKIPPED], S.ELIGIBLE),  # terminal
        ([S.NEEDS_HUMAN], S.WORKING),  # a human must decide first
        (PATH_TO[S.ELIGIBLE], S.NEEDS_HUMAN),
        (PATH_TO[S.ELIGIBLE], S.ELIGIBLE),  # no self loops
    ],
)
def test_rejected_transitions(ledger: Ledger, path: list[AttemptState], target: AttemptState) -> None:
    attempt_id = new_attempt(ledger)
    advance(ledger, attempt_id, path)
    before = ledger.get_attempt(attempt_id)
    count = len(ledger.transitions(attempt_id))
    with pytest.raises(InvalidTransition):
        ledger.transition(attempt_id, target, "should fail")
    assert ledger.get_attempt(attempt_id).state == before.state
    assert len(ledger.transitions(attempt_id)) == count


def test_transition_requires_reason(ledger: Ledger) -> None:
    attempt_id = new_attempt(ledger)
    with pytest.raises(LedgerError, match="reason"):
        ledger.transition(attempt_id, S.ELIGIBLE, "  ")


def test_new_attempt_starts_discovered_with_history(ledger: Ledger) -> None:
    attempt_id = new_attempt(ledger)
    assert ledger.get_attempt(attempt_id).state == S.DISCOVERED
    [first] = ledger.transitions(attempt_id)
    assert first.from_state is None
    assert first.to_state == S.DISCOVERED


def test_unknown_attempt(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="no attempt"):
        ledger.transition(999, S.ELIGIBLE, "x")


def test_attempts_in_state(ledger: Ledger) -> None:
    a = new_attempt(ledger, 1)
    b = new_attempt(ledger, 2)
    ledger.transition(b, S.ELIGIBLE, "ok")
    assert [x.id for x in ledger.attempts_in_state(S.DISCOVERED)] == [a]
    assert [x.id for x in ledger.attempts_in_state(S.ELIGIBLE)] == [b]


def test_repository_and_issue_upsert(ledger: Ledger) -> None:
    repo = ledger.upsert_repository("Owner/Repo", language="java", policy_check={"ok": True})
    assert ledger.upsert_repository("owner/repo", deny_listed=True) == repo
    record = ledger.get_repository("OWNER/REPO")
    assert record is not None
    assert record["language"] == "java"
    assert record["policy_check"] == {"ok": True}
    assert record["deny_listed"] is True
    with pytest.raises(LedgerError, match="unknown repository fields"):
        ledger.upsert_repository("owner/repo", stars=5)

    issue = ledger.upsert_issue(repo, 7, "Old", ["help wanted"], "https://x/7")
    assert ledger.upsert_issue(repo, 7, "New", ["bug"], "https://x/7") == issue
    ledger.set_issue_eligibility(issue, False, [{"check": "open", "passed": False, "reason": "closed"}])
    stored = ledger.get_issue(issue)
    assert stored is not None
    assert stored["title"] == "New"
    assert stored["labels"] == ["bug"]
    assert stored["eligible"] is False
    assert stored["eligibility"][0]["reason"] == "closed"


def test_record_limits_logs_only_changes(ledger: Ledger) -> None:
    assert ledger.record_limits({"prs_per_week": 3, "max_diff_lines": 200}, "initial") == [
        "max_diff_lines",
        "prs_per_week",
    ]
    assert ledger.record_limits({"prs_per_week": 3, "max_diff_lines": 200}, "unchanged") == []
    assert ledger.record_limits({"prs_per_week": 5, "max_diff_lines": 200}, "raised") == ["prs_per_week"]


def test_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    with Ledger(path) as led:
        assert led.schema_version == SCHEMA_VERSION
        new_attempt(led)
    with Ledger(path) as led:
        assert led.schema_version == SCHEMA_VERSION
        assert len(led.attempts_in_state(S.DISCOVERED)) == 1


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "x.sqlite3")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(LedgerError, match="newer"):
        migrate(conn)
    conn.close()


def test_decisions_are_tied_to_content_hash(ledger: Ledger) -> None:
    asked = ledger.request_decision("o/r", "ai_policy", "hash1", "mentions AI")
    assert asked.decision is None
    assert ledger.request_decision("o/r", "ai_policy", "hash1", "again").id == asked.id
    assert ledger.find_decision("o/r", "ai_policy", "hash1") is None
    assert [d.id for d in ledger.pending_decisions()] == [asked.id]

    done = ledger.decide("O/R", "ai_policy", Decision.ALLOW, "read it; AI help is fine")
    assert done.decision is Decision.ALLOW
    found = ledger.find_decision("o/r", "ai_policy", "hash1")
    assert found is not None
    assert found.reason == "read it; AI help is fine"
    assert ledger.pending_decisions() == []
    # Changed policy files: the old decision does not apply.
    assert ledger.find_decision("o/r", "ai_policy", "hash2") is None


def test_decide_requires_open_question_and_reason(ledger: Ledger) -> None:
    with pytest.raises(LedgerError, match="no open question"):
        ledger.decide("o/r", "ai_policy", Decision.DENY, "no")
    ledger.request_decision("o/r", "ai_policy", "h", "q")
    with pytest.raises(LedgerError, match="reason"):
        ledger.decide("o/r", "ai_policy", Decision.DENY, " ")


def test_attempts_for_subject(ledger: Ledger) -> None:
    a = new_attempt(ledger, 1)
    b = new_attempt(ledger, 2)
    ledger.transition(a, S.NEEDS_HUMAN, "unclear")
    ledger.transition(b, S.NEEDS_HUMAN, "unclear")
    assert [x.id for x in ledger.attempts_for_subject("owner/repo", S.NEEDS_HUMAN)] == [a, b]
    assert [x.id for x in ledger.attempts_for_subject("owner/repo#2", S.NEEDS_HUMAN)] == [b]
    assert ledger.attempts_for_subject("other/repo", S.NEEDS_HUMAN) == []


def test_migration_from_version_1_keeps_data(tmp_path: Path) -> None:
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    MIGRATIONS[0](conn)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO repositories (full_name, created_at, updated_at) VALUES ('a/b', 'now', 'now')")
    conn.commit()
    conn.close()
    with Ledger(path) as led:
        assert led.schema_version == SCHEMA_VERSION
        assert led.get_repository("a/b") is not None
        assert led.pending_decisions() == []
