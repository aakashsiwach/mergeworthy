"""Scoreboard snapshots from a fixture ledger. Regenerate with UPDATE_SNAPSHOTS=1, then review the diff."""

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mergeworthy import cli
from mergeworthy.ledger import AttemptState, Ledger
from mergeworthy.scoreboard import build, render_html, render_markdown

SNAPSHOTS = Path(__file__).parent / "snapshots"
GENERATED = datetime(2026, 12, 1, 9, 30, tzinfo=UTC)
S = AttemptState
PATH = [S.ELIGIBLE, S.WORKING, S.SELF_REVIEWED, S.AWAITING_APPROVAL, S.APPROVED]


def fixture_ledger(path: Path) -> Ledger:
    ledger = Ledger(path)
    repo = ledger.upsert_repository("acme/widgets", language="python")

    def attempt(number: int, *states: AttemptState, eligibility: list[dict[str, str]] | None = None) -> int:
        issue = ledger.upsert_issue(
            repo, number, f"Issue {number}", ["good first issue"], f"https://x/{number}"
        )
        if eligibility is not None:
            ledger.set_issue_eligibility(issue, False, eligibility)
        attempt_id = ledger.create_attempt(issue)
        for state in states:
            ledger.transition(attempt_id, state, f"to {state}")
        return attempt_id

    fail = "fail"
    attempt(1, S.SKIPPED, eligibility=[
        {"name": "ai_policy", "status": fail, "reason": "forbids AI"},
        {"name": "recent_commit", "status": fail, "reason": "inactive"},
    ])  # fmt: skip
    attempt(2, S.SKIPPED, eligibility=[{"name": "unassigned", "status": fail, "reason": "assigned"}])
    failed = attempt(3, S.ELIGIBLE, S.WORKING, S.FAILED)
    ledger.update_attempt(failed, spend_usd=0.40)
    attempt(4, S.ELIGIBLE, S.WORKING, S.SELF_REVIEWED, S.REJECTED)
    attempt(5, S.ELIGIBLE, S.WORKING, S.SELF_REVIEWED, S.AWAITING_APPROVAL, S.HUMAN_REJECTED)

    def pr(number: int, final: str, spend: float, created: str, merged: str | None, note: str | None) -> None:
        attempt_id = attempt(number, *PATH)
        ledger.update_attempt(attempt_id, spend_usd=spend)
        ledger.record_submission(
            attempt_id,
            repository="acme/widgets",
            number=100 + number,
            url=f"https://github.com/acme/widgets/pull/{100 + number}",
            head_repository="me/widgets",
            branch=f"b{number}",
            reason="opened",
        )
        [row] = [p for p in ledger.pull_requests() if p["number"] == 100 + number]
        ledger._conn.execute("UPDATE pull_requests SET created_at = ? WHERE id = ?", (created, row["id"]))
        ledger._conn.commit()
        ledger.update_pull_request(row["id"], state=final, merged_at=merged, outcome_notes=note)

    pr(6, "merged", 1.00, "2026-11-01T10:00:00+00:00", "2026-11-04T10:00:00+00:00", None)
    pr(7, "merged", 0.60, "2026-11-02T10:00:00+00:00", "2026-11-03T10:00:00+00:00", None)
    pr(
        8,
        "closed",
        0.50,
        "2026-11-05T10:00:00+00:00",
        None,
        "Fixed differently in #9 <script>alert(1)</script>",
    )
    pr(9, "open", 0.25, "2026-11-20T10:00:00+00:00", None, None)
    return ledger


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    led = fixture_ledger(tmp_path / "ledger.sqlite3")
    yield led  # type: ignore[misc]
    led.close()


def test_numbers(ledger: Ledger) -> None:
    board = build(ledger)
    assert board.issues_considered == 9
    assert board.issues_skipped == 2
    assert board.skip_reasons == {"activity": 1, "policy": 1, "scope": 1}
    assert (board.attempts_started, board.attempts_failed) == (7, 1)
    assert (board.attempts_rejected_by_self_review, board.attempts_rejected_by_human) == (1, 1)
    assert (board.prs_submitted, board.prs_merged, board.prs_closed_without_merge, board.prs_open) == (
        4,
        2,
        1,
        1,
    )
    assert board.merge_rate == pytest.approx(2 / 3)
    assert board.median_days_to_merge == pytest.approx(2.0)  # median of 3 days and 1 day
    assert board.total_cost_usd == pytest.approx(2.75)
    assert board.cost_per_merged_pr_usd == pytest.approx(1.375)
    assert board.feedback == [("acme/widgets#108", "Fixed differently in #9 <script>alert(1)</script>")]


@pytest.mark.parametrize(
    ("name", "render"), [("scoreboard.md", render_markdown), ("index.html", render_html)]
)
def test_snapshot(ledger: Ledger, name: str, render: object) -> None:
    text = render(build(ledger), GENERATED)  # type: ignore[operator]
    snapshot = SNAPSHOTS / name
    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        SNAPSHOTS.mkdir(exist_ok=True)
        snapshot.write_text(text, encoding="utf-8", newline="\n")
    assert text == snapshot.read_text(encoding="utf-8")


def test_markdown_escapes_maintainer_text(ledger: Ledger) -> None:
    text = render_markdown(build(ledger), GENERATED)
    assert "<script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


def test_html_escapes_maintainer_text(ledger: Ledger) -> None:
    page = render_html(build(ledger), GENERATED)
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page


def test_empty_ledger(tmp_path: Path) -> None:
    with Ledger(tmp_path / "empty.sqlite3") as empty:
        text = render_markdown(build(empty), GENERATED)
    assert "| Merge rate (merged / (merged + closed)) | n/a |" in text
    assert "| Per merged pull request | n/a |" in text
    assert "- none yet" in text


def test_cli_writes_both_files(tmp_path: Path) -> None:
    fixture_ledger(tmp_path / "ledger.sqlite3").close()
    cfg = tmp_path / "mergeworthy.toml"
    cfg.write_text(f'ledger_path = "{(tmp_path / "ledger.sqlite3").as_posix()}"\n', encoding="utf-8")
    out = tmp_path / "site"
    result = CliRunner().invoke(cli.app, ["scoreboard", "--output", str(out), "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert (out / "index.html").read_text(encoding="utf-8").startswith("<!doctype html>")
    assert "| Merged | 2 |" in (out / "scoreboard.md").read_text(encoding="utf-8")
