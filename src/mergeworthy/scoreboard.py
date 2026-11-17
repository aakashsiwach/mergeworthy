"""The public scoreboard (docs/evaluation.md section 2), as Markdown and static HTML.

Every number comes from the ledger. Failures are shown with the same prominence as successes. Text that came
from maintainers or issues is HTML-escaped in both formats (Markdown renderers accept raw HTML) and kept on
one line.
"""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from mergeworthy.approve import clean
from mergeworthy.ledger import AttemptState, Ledger

# Eligibility checks grouped into the categories evaluation.md asks for.
CATEGORIES = {
    "policy": {
        "public",
        "license",
        "ai_policy",
        "contributing_guide",
        "not_denied",
        "cooldown",
        "claim_first",
    },
    "activity": {"recent_commit", "outside_merge", "not_archived"},
    "tests": {"tests_in_sandbox", "baseline"},
    "scope": {
        "help_label",
        "open",
        "unassigned",
        "no_open_pull_request",
        "no_recent_claim",
        "specific",
        "language",
    },
}


def category(check: str) -> str:
    for name, checks in CATEGORIES.items():
        if check in checks:
            return name
    return "other"


@dataclass
class Scoreboard:
    issues_considered: int = 0
    issues_skipped: int = 0
    skip_reasons: dict[str, int] = field(default_factory=dict)
    attempts_started: int = 0
    attempts_failed: int = 0
    attempts_rejected_by_self_review: int = 0
    attempts_rejected_by_human: int = 0
    attempts_abandoned: int = 0
    prs_submitted: int = 0
    prs_merged: int = 0
    prs_closed_without_merge: int = 0
    prs_open: int = 0
    merge_rate: float | None = None
    median_days_to_merge: float | None = None
    feedback: list[tuple[str, str]] = field(default_factory=list)
    total_cost_usd: float = 0.0
    cost_per_merged_pr_usd: float | None = None


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def build(ledger: Ledger) -> Scoreboard:
    board = Scoreboard()
    board.issues_considered = ledger.select("SELECT COUNT(DISTINCT issue_id) AS n FROM attempts")[0]["n"]

    skipped = ledger.select(
        "SELECT i.eligibility FROM attempts a JOIN issues i ON i.id = a.issue_id WHERE a.state = ?",
        (AttemptState.SKIPPED.value,),
    )
    board.issues_skipped = len(skipped)
    reasons: Counter[str] = Counter()
    for row in skipped:
        checks = json.loads(row["eligibility"]) if row["eligibility"] else []
        failing = {category(c["name"]) for c in checks if c.get("status") in ("fail", "unclear")}
        reasons.update(failing or {"other"})
    board.skip_reasons = dict(sorted(reasons.items()))

    def count_state(state: AttemptState) -> int:
        return int(
            ledger.select("SELECT COUNT(*) AS n FROM attempts WHERE state = ?", (state.value,))[0]["n"]
        )

    board.attempts_started = int(
        ledger.select(
            "SELECT COUNT(DISTINCT attempt_id) AS n FROM transitions WHERE to_state = ?",
            (AttemptState.WORKING.value,),
        )[0]["n"]
    )
    board.attempts_failed = count_state(AttemptState.FAILED)
    board.attempts_rejected_by_self_review = count_state(AttemptState.REJECTED)
    board.attempts_rejected_by_human = count_state(AttemptState.HUMAN_REJECTED)
    board.attempts_abandoned = count_state(AttemptState.ABANDONED)

    prs = ledger.select("SELECT * FROM pull_requests ORDER BY id")
    board.prs_submitted = len(prs)
    merged = [p for p in prs if p["state"] == "merged"]
    closed = [p for p in prs if p["state"] == "closed"]
    board.prs_merged, board.prs_closed_without_merge = len(merged), len(closed)
    board.prs_open = len([p for p in prs if p["state"] == "open"])
    decided = board.prs_merged + board.prs_closed_without_merge
    board.merge_rate = board.prs_merged / decided if decided else None
    durations = []
    for p in merged:
        start, end = _parse(p["created_at"]), _parse(p["merged_at"])
        if start and end:
            durations.append((end - start).total_seconds() / 86400)
    board.median_days_to_merge = statistics.median(durations) if durations else None
    for p in closed:
        ref = f"{p['repository']}#{p['number']}"
        board.feedback.append((ref, " ".join(clean(p["outcome_notes"] or "no reason given").split())))

    board.total_cost_usd = float(
        ledger.select("SELECT COALESCE(SUM(spend_usd), 0) AS s FROM attempts")[0]["s"]
    )
    board.cost_per_merged_pr_usd = board.total_cost_usd / board.prs_merged if board.prs_merged else None
    return board


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def _days(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} days"


def rows(board: Scoreboard) -> list[tuple[str, list[tuple[str, str]]]]:
    reasons = ", ".join(f"{k} {v}" for k, v in board.skip_reasons.items()) or "none"
    return [
        (
            "Issues",
            [
                ("Considered", str(board.issues_considered)),
                ("Skipped", str(board.issues_skipped)),
                ("Skip reasons", reasons),
            ],
        ),
        (
            "Attempts",
            [
                ("Started", str(board.attempts_started)),
                ("Failed", str(board.attempts_failed)),
                ("Rejected by self-review", str(board.attempts_rejected_by_self_review)),
                ("Rejected by the human", str(board.attempts_rejected_by_human)),
                ("Abandoned (issue taken or closed)", str(board.attempts_abandoned)),
            ],
        ),
        (
            "Pull requests",
            [
                ("Submitted", str(board.prs_submitted)),
                ("Merged", str(board.prs_merged)),
                ("Closed without merge", str(board.prs_closed_without_merge)),
                ("Still open", str(board.prs_open)),
                ("Merge rate (merged / (merged + closed))", _pct(board.merge_rate)),
                ("Median time to merge", _days(board.median_days_to_merge)),
            ],
        ),
        (
            "Cost",
            [
                ("Total", _usd(board.total_cost_usd)),
                ("Per merged pull request", _usd(board.cost_per_merged_pr_usd)),
            ],
        ),
    ]


def render_markdown(board: Scoreboard, generated_at: datetime) -> str:
    out = [
        "# mergeworthy scoreboard",
        "",
        f"Generated {generated_at.isoformat(timespec='minutes')} from the ledger.",
        "",
    ]
    for title, items in rows(board):
        out += [f"## {title}", "", "| | |", "|---|---|"]
        out += [f"| {label} | {value} |" for label, value in items]
        out.append("")
    out += ["## Maintainer feedback on closed pull requests", ""]
    if board.feedback:
        for ref, note in board.feedback:
            safe = html.escape(note, quote=False).replace("|", "\\|").replace("`", "'")
            out.append(f"- {ref}: {safe}")
    else:
        out.append("- none yet")
    out.append("")
    return "\n".join(out)


def render_html(board: Scoreboard, generated_at: datetime) -> str:
    def e(text: str) -> str:
        return html.escape(text, quote=True)

    sections = []
    for title, items in rows(board):
        body = "".join(
            f'<tr><th scope="row">{e(label)}</th><td>{e(value)}</td></tr>' for label, value in items
        )
        sections.append(f"<section><h2>{e(title)}</h2><table>{body}</table></section>")
    feedback = (
        "".join(f"<li><strong>{e(ref)}</strong>: {e(note)}</li>" for ref, note in board.feedback)
        or "<li>none yet</li>"
    )
    sections.append(
        f"<section><h2>Maintainer feedback on closed pull requests</h2><ul>{feedback}</ul></section>"
    )
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>mergeworthy scoreboard</title>\n"
        "<style>body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem}"
        "table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:.3rem .5rem;"
        "border-bottom:1px solid #ddd}th{font-weight:normal}td{font-variant-numeric:tabular-nums}</style>\n"
        "</head>\n<body>\n<h1>mergeworthy scoreboard</h1>\n"
        f"<p>Generated {e(generated_at.isoformat(timespec='minutes'))} from the ledger. "
        "Failures are listed with the same prominence as successes.</p>\n"
        + "\n".join(sections)
        + "\n</body>\n</html>\n"
    )


def write(ledger: Ledger, output: Path, generated_at: datetime) -> tuple[Path, Path]:
    board = build(ledger)
    output.mkdir(parents=True, exist_ok=True)
    md, page = output / "scoreboard.md", output / "index.html"
    md.write_text(render_markdown(board, generated_at), encoding="utf-8", newline="\n")
    page.write_text(render_html(board, generated_at), encoding="utf-8", newline="\n")
    return md, page


def as_dict(board: Scoreboard) -> dict[str, Any]:
    return dict(board.__dict__)
