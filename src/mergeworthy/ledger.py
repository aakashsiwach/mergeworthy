"""SQLite ledger: repositories, issues, attempts, state transitions, pull requests and limit changes.

The schema follows docs/architecture.md. Migrations are numbered and tracked with ``PRAGMA user_version``.
Attempt states move only along the edges of the state diagram in docs/architecture.md.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Self


class AttemptState(StrEnum):
    DISCOVERED = "discovered"
    NEEDS_HUMAN = "needs_human"
    ELIGIBLE = "eligible"
    SKIPPED = "skipped"
    CLAIM_DRAFTED = "claim_drafted"
    CLAIM_APPROVED = "claim_approved"
    CLAIMED = "claimed"
    WORKING = "working"
    FAILED = "failed"
    SELF_REVIEWED = "self_reviewed"
    REJECTED = "rejected"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    HUMAN_REJECTED = "human_rejected"
    SUBMITTED = "submitted"
    IN_REVIEW = "in_review"
    MERGED = "merged"
    CLOSED = "closed"
    DECLINED = "declined"
    ABANDONED = "abandoned"


S = AttemptState

TERMINAL_STATES = frozenset(
    {S.SKIPPED, S.FAILED, S.REJECTED, S.HUMAN_REJECTED, S.MERGED, S.CLOSED, S.DECLINED, S.ABANDONED}
)

# Edges of the diagram in docs/architecture.md, and nothing else.
_EDGES: dict[AttemptState, set[AttemptState]] = {
    S.DISCOVERED: {S.ELIGIBLE, S.SKIPPED, S.NEEDS_HUMAN},
    S.NEEDS_HUMAN: {S.ELIGIBLE, S.SKIPPED},
    S.ELIGIBLE: {S.CLAIM_DRAFTED, S.WORKING, S.SKIPPED},
    S.CLAIM_DRAFTED: {S.CLAIM_APPROVED, S.HUMAN_REJECTED},
    S.CLAIM_APPROVED: {S.CLAIMED, S.DECLINED},
    S.CLAIMED: {S.WORKING, S.DECLINED},
    S.WORKING: {S.SELF_REVIEWED, S.FAILED},
    S.SELF_REVIEWED: {S.AWAITING_APPROVAL, S.REJECTED},
    S.AWAITING_APPROVAL: {S.APPROVED, S.HUMAN_REJECTED},
    S.APPROVED: {S.SUBMITTED},
    S.SUBMITTED: {S.IN_REVIEW, S.CLOSED},
    S.IN_REVIEW: {S.MERGED, S.CLOSED},
}
# Any non-terminal attempt can be abandoned: the issue was closed, assigned elsewhere, or a pull request
# linked to it appeared.
for _targets in _EDGES.values():
    _targets.add(S.ABANDONED)

ALLOWED_TRANSITIONS: Mapping[AttemptState, frozenset[AttemptState]] = {
    state: frozenset(_EDGES.get(state, set())) for state in AttemptState
}
assert all(not ALLOWED_TRANSITIONS[s] for s in TERMINAL_STATES)
assert all(ALLOWED_TRANSITIONS[s] for s in set(AttemptState) - TERMINAL_STATES)


_JSON_FIELDS = ("diff_stats", "test_results", "review_reasons", "commands", "verification", "approved_files")


class LedgerError(Exception):
    pass


class InvalidTransition(LedgerError):
    def __init__(self, attempt_id: int, current: AttemptState, target: AttemptState) -> None:
        super().__init__(f"attempt {attempt_id}: transition {current} -> {target} is not allowed")
        self.attempt_id = attempt_id
        self.current = current
        self.target = target


def is_allowed_transition(current: AttemptState, target: AttemptState) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


# --- migrations -------------------------------------------------------------------------------------------


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE repositories (
            id              INTEGER PRIMARY KEY,
            full_name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
            language        TEXT,
            license         TEXT,
            default_branch  TEXT,
            activity_checks TEXT,              -- JSON
            policy_check    TEXT,              -- JSON
            deny_listed     INTEGER NOT NULL DEFAULT 0,
            cooldown_until  TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );

        CREATE TABLE issues (
            id             INTEGER PRIMARY KEY,
            repository_id  INTEGER NOT NULL REFERENCES repositories(id),
            number         INTEGER NOT NULL,
            title          TEXT NOT NULL,
            labels         TEXT NOT NULL,      -- JSON list
            url            TEXT NOT NULL,
            eligible       INTEGER,            -- NULL until checked
            eligibility    TEXT,               -- JSON list of check results
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL,
            UNIQUE (repository_id, number)
        );

        CREATE TABLE attempts (
            id              INTEGER PRIMARY KEY,
            issue_id        INTEGER NOT NULL REFERENCES issues(id),
            state           TEXT NOT NULL,
            branch          TEXT,
            diff_stats      TEXT,              -- JSON
            test_results    TEXT,              -- JSON
            review_verdict  TEXT,
            review_reasons  TEXT,              -- JSON
            spend_usd       REAL NOT NULL DEFAULT 0,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL
        );
        CREATE INDEX attempts_state ON attempts(state);

        CREATE TABLE transitions (
            id          INTEGER PRIMARY KEY,
            attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
            from_state  TEXT,
            to_state    TEXT NOT NULL,
            reason      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        CREATE INDEX transitions_attempt ON transitions(attempt_id);

        CREATE TABLE pull_requests (
            id             INTEGER PRIMARY KEY,
            attempt_id     INTEGER NOT NULL UNIQUE REFERENCES attempts(id),
            url            TEXT NOT NULL,
            state          TEXT NOT NULL,
            ci_status      TEXT,
            merged_at      TEXT,
            closed_at      TEXT,
            outcome_notes  TEXT,
            created_at     TEXT NOT NULL,
            updated_at     TEXT NOT NULL
        );

        CREATE TABLE limits_log (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT NOT NULL,
            reason      TEXT NOT NULL,
            created_at  TEXT NOT NULL
        );
        """
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- A question for a human (decision NULL) or the human's answer. The content hash identifies what the
        -- question was about, e.g. the repository's policy files; a changed hash means asking again.
        CREATE TABLE human_decisions (
            id            INTEGER PRIMARY KEY,
            subject       TEXT NOT NULL COLLATE NOCASE,   -- owner/repo or owner/repo#number
            check_name    TEXT NOT NULL,
            content_hash  TEXT NOT NULL,
            question      TEXT NOT NULL,
            decision      TEXT CHECK (decision IN ('allow', 'deny')),
            reason        TEXT,
            requested_at  TEXT NOT NULL,
            decided_at    TEXT,
            UNIQUE (subject, check_name, content_hash)
        );
        """
    )


# Append new migrations; never edit or reorder an existing one.
def _migration_3(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- What the agent produced, kept as text: approval shows it and submission applies exactly this diff.
        ALTER TABLE attempts ADD COLUMN diff TEXT;
        ALTER TABLE attempts ADD COLUMN explanation TEXT;
        ALTER TABLE attempts ADD COLUMN commands TEXT;       -- JSON list
        ALTER TABLE attempts ADD COLUMN verification TEXT;   -- JSON list of rule results
        """
    )


def _migration_4(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- What exactly a human approved: submission must send this diff and these files, nothing else.
        ALTER TABLE attempts ADD COLUMN approved_diff_sha256 TEXT;
        ALTER TABLE attempts ADD COLUMN approved_files TEXT;  -- JSON list
        ALTER TABLE attempts ADD COLUMN approved_at TEXT;
        """
    )


def _migration_5(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE attempts ADD COLUMN base_commit TEXT;        -- the upstream commit the change is based on
        ALTER TABLE pull_requests ADD COLUMN repository TEXT;     -- upstream owner/name
        ALTER TABLE pull_requests ADD COLUMN number INTEGER;
        ALTER TABLE pull_requests ADD COLUMN head_repository TEXT;
        ALTER TABLE pull_requests ADD COLUMN branch TEXT;
        """
    )


def _migration_6(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- Follow-up actions drafted for a human: replies, fix attempts, closing.
        -- Nothing is sent without approval.
        CREATE TABLE drafts (
            id          INTEGER PRIMARY KEY,
            attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
            kind        TEXT NOT NULL CHECK (kind IN ('reply', 'fix', 'close')),
            source      TEXT NOT NULL,       -- what prompted it, e.g. comment:123, review:5, ci:<sha>
            context     TEXT NOT NULL,       -- JSON: the comment, failing checks, ...
            body        TEXT,                -- proposed text, written or edited by a human before approval
            status      TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'approved', 'rejected', 'sent')),
            created_at  TEXT NOT NULL,
            UNIQUE (attempt_id, source)
        );
        """
    )


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
    _migration_6,
)
SCHEMA_VERSION = len(MIGRATIONS)


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order. Returns the resulting schema version."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise LedgerError(f"ledger schema version {version} is newer than this program ({SCHEMA_VERSION})")
    for number in range(version + 1, SCHEMA_VERSION + 1):
        with conn:
            MIGRATIONS[number - 1](conn)
            conn.execute(f"PRAGMA user_version = {number}")
    return SCHEMA_VERSION


# --- records ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    id: int
    issue_id: int
    state: AttemptState
    spend_usd: float
    created_at: str
    updated_at: str


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class DecisionRecord:
    id: int
    subject: str
    check_name: str
    content_hash: str
    question: str
    decision: Decision | None
    reason: str | None
    requested_at: str
    decided_at: str | None


@dataclass(frozen=True)
class Transition:
    attempt_id: int
    from_state: AttemptState | None
    to_state: AttemptState
    reason: str
    created_at: str


class Ledger:
    """A connection to the ledger database. Use as a context manager or call ``close``."""

    def __init__(self, path: Path | str) -> None:
        self.path = path
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        migrate(self._conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    @property
    def schema_version(self) -> int:
        return int(self._conn.execute("PRAGMA user_version").fetchone()[0])

    # repositories

    def upsert_repository(self, full_name: str, **fields: Any) -> int:
        allowed = {
            "language",
            "license",
            "default_branch",
            "activity_checks",
            "policy_check",
            "deny_listed",
            "cooldown_until",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise LedgerError(f"unknown repository fields: {sorted(unknown)}")
        values = {k: _json(v) if k in {"activity_checks", "policy_check"} else v for k, v in fields.items()}
        if "deny_listed" in values:
            values["deny_listed"] = int(bool(values["deny_listed"]))
        now = _now()
        with self._tx() as conn:
            row = conn.execute("SELECT id FROM repositories WHERE full_name = ?", (full_name,)).fetchone()
            if row is None:
                cols = ["full_name", "created_at", "updated_at", *values]
                cur = conn.execute(
                    f"INSERT INTO repositories ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                    (full_name, now, now, *values.values()),
                )
                return int(cur.lastrowid or 0)
            repo_id = int(row["id"])
            if values:
                sets = ", ".join(f"{k} = ?" for k in values)
                conn.execute(
                    f"UPDATE repositories SET {sets}, updated_at = ? WHERE id = ?",
                    (*values.values(), now, repo_id),
                )
            return repo_id

    def get_repository(self, full_name: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM repositories WHERE full_name = ?", (full_name,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        for key in ("activity_checks", "policy_check"):
            if record[key] is not None:
                record[key] = json.loads(record[key])
        record["deny_listed"] = bool(record["deny_listed"])
        return record

    # issues

    def upsert_issue(self, repository_id: int, number: int, title: str, labels: list[str], url: str) -> int:
        now = _now()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO issues (repository_id, number, title, labels, url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (repository_id, number) DO UPDATE SET
                    title = excluded.title, labels = excluded.labels, url = excluded.url,
                    updated_at = excluded.updated_at
                """,
                (repository_id, number, title, json.dumps(labels), url, now, now),
            )
            row = conn.execute(
                "SELECT id FROM issues WHERE repository_id = ? AND number = ?", (repository_id, number)
            ).fetchone()
            return int(row["id"])

    def set_issue_eligibility(self, issue_id: int, eligible: bool, results: list[dict[str, Any]]) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE issues SET eligible = ?, eligibility = ?, updated_at = ? WHERE id = ?",
                (int(eligible), json.dumps(results), _now(), issue_id),
            )

    def get_issue(self, issue_id: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["labels"] = json.loads(record["labels"])
        if record["eligibility"] is not None:
            record["eligibility"] = json.loads(record["eligibility"])
        if record["eligible"] is not None:
            record["eligible"] = bool(record["eligible"])
        return record

    # attempts and transitions

    def create_attempt(self, issue_id: int, reason: str = "issue discovered") -> int:
        if not reason.strip():
            raise LedgerError("a reason is required")
        now = _now()
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT INTO attempts (issue_id, state, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (issue_id, S.DISCOVERED.value, now, now),
            )
            attempt_id = int(cur.lastrowid or 0)
            conn.execute(
                "INSERT INTO transitions (attempt_id, from_state, to_state, reason, created_at)"
                " VALUES (?, NULL, ?, ?, ?)",
                (attempt_id, S.DISCOVERED.value, reason, now),
            )
        return attempt_id

    def update_attempt(self, attempt_id: int, **fields: Any) -> None:
        """Record results on an attempt: ``branch``, ``diff_stats``, ``test_results``, ``spend_usd``."""
        allowed = {
            "branch",
            "diff_stats",
            "test_results",
            "spend_usd",
            "diff",
            "explanation",
            "commands",
            "verification",
            "review_verdict",
            "review_reasons",
            "base_commit",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise LedgerError(f"unknown attempt fields: {sorted(unknown)}")
        if not fields:
            return
        values = {k: _json(v) if k in _JSON_FIELDS else v for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._tx() as conn:
            cur = conn.execute(
                f"UPDATE attempts SET {sets}, updated_at = ? WHERE id = ?",
                (*values.values(), _now(), attempt_id),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"no attempt with id {attempt_id}")

    def record_approval(self, attempt_id: int, diff_sha256: str, files: list[str], reason: str) -> Attempt:
        """Move awaiting_approval to approved and pin the exact diff and files that were approved."""
        if not reason.strip():
            raise LedgerError("a reason is required for every approval")
        if not re.fullmatch(r"[0-9a-f]{64}", diff_sha256):
            raise LedgerError("diff_sha256 must be a SHA-256 hex digest")
        now = _now()
        with self._tx() as conn:
            current = self.get_attempt(attempt_id).state
            if not is_allowed_transition(current, AttemptState.APPROVED):
                raise InvalidTransition(attempt_id, current, AttemptState.APPROVED)
            cur = conn.execute(
                "UPDATE attempts SET state = ?, approved_diff_sha256 = ?, approved_files = ?,"
                " approved_at = ?, updated_at = ? WHERE id = ? AND state = ?",
                (
                    S.APPROVED.value,
                    diff_sha256,
                    json.dumps(sorted(files)),
                    now,
                    now,
                    attempt_id,
                    current.value,
                ),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"attempt {attempt_id} changed state concurrently")
            conn.execute(
                "INSERT INTO transitions (attempt_id, from_state, to_state, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, current.value, S.APPROVED.value, f"{reason} (diff sha256 {diff_sha256})", now),
            )
        return self.get_attempt(attempt_id)

    def record_submission(
        self,
        attempt_id: int,
        *,
        repository: str,
        number: int,
        url: str,
        head_repository: str,
        branch: str,
        reason: str,
    ) -> int:
        """Record the opened pull request and move the attempt from ``approved`` to ``submitted``."""
        now = _now()
        with self._tx() as conn:
            current = self.get_attempt(attempt_id).state
            if not is_allowed_transition(current, AttemptState.SUBMITTED):
                raise InvalidTransition(attempt_id, current, AttemptState.SUBMITTED)
            cur = conn.execute(
                "INSERT INTO pull_requests (attempt_id, url, state, repository, number, head_repository,"
                " branch, created_at, updated_at) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (attempt_id, url, repository, number, head_repository, branch, now, now),
            )
            conn.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (S.SUBMITTED.value, now, attempt_id, current.value),
            )
            conn.execute(
                "INSERT INTO transitions (attempt_id, from_state, to_state, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, current.value, S.SUBMITTED.value, reason, now),
            )
            return int(cur.lastrowid or 0)

    def pull_requests(
        self, *, state: str | None = None, repository: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM pull_requests WHERE 1 = 1"
        params: list[Any] = []
        if state is not None:
            query += " AND state = ?"
            params.append(state)
        if repository is not None:
            query += " AND repository = ? COLLATE NOCASE"
            params.append(repository)
        return [dict(row) for row in self._conn.execute(query + " ORDER BY id", params).fetchall()]

    def pull_requests_since(self, since_iso: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) FROM pull_requests WHERE created_at >= ?", (since_iso,)
        ).fetchone()
        return int(row[0])

    def update_pull_request(self, pr_id: int, **fields: Any) -> None:
        allowed = {"state", "ci_status", "merged_at", "closed_at", "outcome_notes"}
        unknown = set(fields) - allowed
        if unknown:
            raise LedgerError(f"unknown pull request fields: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self._tx() as conn:
            conn.execute(
                f"UPDATE pull_requests SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), _now(), pr_id),
            )

    def add_draft(
        self, attempt_id: int, kind: str, source: str, context: dict[str, Any], body: str | None = None
    ) -> bool:
        """Store a draft once per source. Returns False if this source was already drafted."""
        with self._tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO drafts (attempt_id, kind, source, context, body, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (attempt_id, kind, source, json.dumps(context, sort_keys=True), body, _now()),
            )
            return cur.rowcount == 1

    def drafts(self, status: str | None = "pending") -> list[dict[str, Any]]:
        query = "SELECT * FROM drafts" + (" WHERE status = ?" if status else "") + " ORDER BY id"
        rows = self._conn.execute(query, (status,) if status else ()).fetchall()
        return [{**dict(r), "context": json.loads(r["context"])} for r in rows]

    def select(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Read-only queries for reports. Only a single SELECT statement is accepted."""
        statement = sql.strip().rstrip(";")
        if not statement.upper().startswith("SELECT") or ";" in statement:
            raise LedgerError("only a single SELECT statement is allowed")
        return [dict(row) for row in self._conn.execute(statement, params).fetchall()]

    def attempt_details(self, attempt_id: int) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no attempt with id {attempt_id}")
        record = dict(row)
        for key in _JSON_FIELDS:
            if record[key] is not None:
                record[key] = json.loads(record[key])
        return record

    def attempts_for_issue(self, issue_id: int) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE issue_id = ? ORDER BY id", (issue_id,)
        ).fetchall()
        return [self._attempt(row) for row in rows]

    def get_attempt(self, attempt_id: int) -> Attempt:
        row = self._conn.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if row is None:
            raise LedgerError(f"no attempt with id {attempt_id}")
        return self._attempt(row)

    def attempts_in_state(self, state: AttemptState) -> list[Attempt]:
        rows = self._conn.execute(
            "SELECT * FROM attempts WHERE state = ? ORDER BY id", (state.value,)
        ).fetchall()
        return [self._attempt(row) for row in rows]

    def transition(self, attempt_id: int, target: AttemptState, reason: str) -> Attempt:
        """Move an attempt to ``target``. Raises ``InvalidTransition`` for edges not in the diagram."""
        if not reason.strip():
            raise LedgerError("a reason is required for every transition")
        now = _now()
        with self._tx() as conn:
            current = self.get_attempt(attempt_id).state
            if not is_allowed_transition(current, target):
                raise InvalidTransition(attempt_id, current, target)
            cur = conn.execute(
                "UPDATE attempts SET state = ?, updated_at = ? WHERE id = ? AND state = ?",
                (target.value, now, attempt_id, current.value),
            )
            if cur.rowcount != 1:
                raise LedgerError(f"attempt {attempt_id} changed state concurrently")
            conn.execute(
                "INSERT INTO transitions (attempt_id, from_state, to_state, reason, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (attempt_id, current.value, target.value, reason, now),
            )
        return self.get_attempt(attempt_id)

    def transitions(self, attempt_id: int) -> list[Transition]:
        rows = self._conn.execute(
            "SELECT * FROM transitions WHERE attempt_id = ? ORDER BY id", (attempt_id,)
        ).fetchall()
        return [
            Transition(
                attempt_id=int(r["attempt_id"]),
                from_state=AttemptState(r["from_state"]) if r["from_state"] else None,
                to_state=AttemptState(r["to_state"]),
                reason=r["reason"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # human decisions

    def request_decision(
        self, subject: str, check_name: str, content_hash: str, question: str
    ) -> DecisionRecord:
        """Record a question for a human, unless the same question (same content hash) already exists."""
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO human_decisions"
                " (subject, check_name, content_hash, question, requested_at) VALUES (?, ?, ?, ?, ?)",
                (subject, check_name, content_hash, question, _now()),
            )
        record = self._decision_row(subject, check_name, content_hash)
        assert record is not None
        return record

    def find_decision(self, subject: str, check_name: str, content_hash: str) -> DecisionRecord | None:
        """The human's answer for exactly this content, or None if there is none yet."""
        record = self._decision_row(subject, check_name, content_hash)
        return record if record is not None and record.decision is not None else None

    def decide(self, subject: str, check_name: str, decision: Decision, reason: str) -> DecisionRecord:
        """Answer the newest open question for ``subject`` and ``check_name``."""
        if not reason.strip():
            raise LedgerError("a reason is required for every decision")
        with self._tx() as conn:
            row = conn.execute(
                "SELECT id FROM human_decisions WHERE subject = ? AND check_name = ? AND decision IS NULL"
                " ORDER BY id DESC LIMIT 1",
                (subject, check_name),
            ).fetchone()
            if row is None:
                raise LedgerError(f"no open question for {subject} {check_name}")
            conn.execute(
                "UPDATE human_decisions SET decision = ?, reason = ?, decided_at = ? WHERE id = ?",
                (decision.value, reason, _now(), row["id"]),
            )
            done = conn.execute("SELECT * FROM human_decisions WHERE id = ?", (row["id"],)).fetchone()
        return self._decision(done)

    def pending_decisions(self) -> list[DecisionRecord]:
        rows = self._conn.execute(
            "SELECT * FROM human_decisions WHERE decision IS NULL ORDER BY id"
        ).fetchall()
        return [self._decision(row) for row in rows]

    def attempts_for_subject(self, subject: str, state: AttemptState | None = None) -> list[Attempt]:
        """Attempts for a repository (``owner/repo``) or an issue (``owner/repo#n``), optionally by state."""
        name, _, number = subject.partition("#")
        query = (
            "SELECT a.* FROM attempts a JOIN issues i ON i.id = a.issue_id"
            " JOIN repositories r ON r.id = i.repository_id WHERE r.full_name = ?"
        )
        params: list[Any] = [name]
        if state is not None:
            query += " AND a.state = ?"
            params.append(state.value)
        if number:
            query += " AND i.number = ?"
            params.append(int(number))
        rows = self._conn.execute(query + " ORDER BY a.id", params).fetchall()
        return [self._attempt(row) for row in rows]

    def _decision_row(self, subject: str, check_name: str, content_hash: str) -> DecisionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM human_decisions WHERE subject = ? AND check_name = ? AND content_hash = ?",
            (subject, check_name, content_hash),
        ).fetchone()
        return None if row is None else self._decision(row)

    @staticmethod
    def _decision(row: sqlite3.Row) -> DecisionRecord:
        return DecisionRecord(
            id=int(row["id"]),
            subject=row["subject"],
            check_name=row["check_name"],
            content_hash=row["content_hash"],
            question=row["question"],
            decision=Decision(row["decision"]) if row["decision"] else None,
            reason=row["reason"],
            requested_at=row["requested_at"],
            decided_at=row["decided_at"],
        )

    # limits

    def record_limits(self, limits: Mapping[str, Any], reason: str) -> list[str]:
        """Log every limit whose value differs from the last recorded value. Returns the changed names."""
        if not reason.strip():
            raise LedgerError("a reason is required")
        changed = []
        now = _now()
        with self._tx() as conn:
            for name, value in sorted(limits.items()):
                new = json.dumps(value)
                row = conn.execute(
                    "SELECT new_value FROM limits_log WHERE name = ? ORDER BY id DESC LIMIT 1", (name,)
                ).fetchone()
                old = None if row is None else row["new_value"]
                if old == new:
                    continue
                conn.execute(
                    "INSERT INTO limits_log (name, old_value, new_value, reason, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (name, old, new, reason, now),
                )
                changed.append(name)
        return changed

    @staticmethod
    def _attempt(row: sqlite3.Row) -> Attempt:
        return Attempt(
            id=int(row["id"]),
            issue_id=int(row["issue_id"]),
            state=AttemptState(row["state"]),
            spend_usd=float(row["spend_usd"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
