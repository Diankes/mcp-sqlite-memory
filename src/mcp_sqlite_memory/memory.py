"""Append-only, hash-chained event log plus checkpoints.

The server computes every hash itself: sha256 over the canonical JSON array
[id, ts, session, kind, content, prev_hash]. Each row carries the hash of the
row before it, so editing, deleting or reordering any row breaks the chain from
that point on, and verify() names the first row where it breaks.
"""

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from mcp_sqlite_memory.db import Database, utc_now
from mcp_sqlite_memory.errors import UserError

GENESIS = "0" * 64


def event_hash(
    event_id: int, ts: str, session: str, kind: str, content: str, prev_hash: str
) -> str:
    payload = json.dumps(
        [event_id, ts, session, kind, content, prev_hash],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class ChainStatus:
    ok: bool
    events: int
    head_id: int
    head_hash: str
    break_at_id: int | None = None
    reason: str | None = None

    def to_json(self) -> str:
        data = asdict(self)
        if self.ok:
            del data["break_at_id"]
            del data["reason"]
        return json.dumps(data)

    def summary(self) -> str:
        if self.ok:
            return f"chain ok ({self.events} events)"
        return f"chain BROKEN at event {self.break_at_id}: {self.reason}"


@dataclass
class Checkpoint:
    id: int
    ts: str
    session: str
    summary: str
    last_event_id: int
    head_hash: str


class Memory:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, content: str, kind: str = "note") -> tuple[int, str]:
        content = content.strip()
        if not content:
            raise UserError("content must not be empty")
        kind = (kind or "note").strip() or "note"
        with self.db.internal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                prev_id, prev_hash = self._head(conn)
                event_id = prev_id + 1
                ts = utc_now()
                digest = event_hash(event_id, ts, self.db.session, kind, content, prev_hash)
                conn.execute(
                    "INSERT INTO memory_events (id, ts, session, kind, content, prev_hash, hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (event_id, ts, self.db.session, kind, content, prev_hash, digest),
                )
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return event_id, digest

    @staticmethod
    def _head(conn: sqlite3.Connection) -> tuple[int, str]:
        row = conn.execute("SELECT id, hash FROM memory_events ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (0, GENESIS)

    def verify(self) -> ChainStatus:
        with self.db.internal() as conn:
            total = conn.execute("SELECT count(*) FROM memory_events").fetchone()[0]
            expected_id, expected_prev = 1, GENESIS
            head_id, head_hash = 0, GENESIS
            cursor = conn.execute(
                "SELECT id, ts, session, kind, content, prev_hash, hash "
                "FROM memory_events ORDER BY id"
            )
            for event_id, ts, session, kind, content, prev_hash, digest in cursor:
                if event_id != expected_id:
                    return ChainStatus(
                        False,
                        total,
                        head_id,
                        head_hash,
                        event_id,
                        f"id gap: expected event {expected_id}, found {event_id} "
                        "(rows were deleted or renumbered)",
                    )
                if prev_hash != expected_prev:
                    return ChainStatus(
                        False,
                        total,
                        head_id,
                        head_hash,
                        event_id,
                        f"prev_hash does not match the hash of event {event_id - 1}",
                    )
                if digest != event_hash(event_id, ts, session, kind, content, prev_hash):
                    return ChainStatus(
                        False,
                        total,
                        head_id,
                        head_hash,
                        event_id,
                        "stored hash does not match the recomputed hash "
                        "(content or metadata was altered)",
                    )
                expected_id += 1
                expected_prev = digest
                head_id, head_hash = event_id, digest
        return ChainStatus(True, total, head_id, head_hash)

    def checkpoint(self, summary: str) -> Checkpoint:
        summary = summary.strip()
        if not summary:
            raise UserError("summary must not be empty")
        with self.db.internal() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                head_id, head_hash = self._head(conn)
                ts = utc_now()
                cur = conn.execute(
                    "INSERT INTO memory_checkpoints "
                    "(ts, session, summary, last_event_id, head_hash) VALUES (?, ?, ?, ?, ?)",
                    (ts, self.db.session, summary, head_id, head_hash),
                )
                checkpoint_id = cur.lastrowid or 0
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return Checkpoint(checkpoint_id, ts, self.db.session, summary, head_id, head_hash)

    def latest_checkpoint(self) -> Checkpoint | None:
        with self.db.internal() as conn:
            row = conn.execute(
                "SELECT id, ts, session, summary, last_event_id, head_hash "
                "FROM memory_checkpoints ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return Checkpoint(*row) if row else None

    def events_after(
        self, event_id: int, max_events: int, exclude_kinds: Sequence[str] = ()
    ) -> tuple[list[tuple], int]:
        """The most recent max_events events after event_id, oldest first, plus the total.

        exclude_kinds leaves out kinds (for example hook-written raw events); the total
        counts only the kinds that remain."""
        excluded = [kind for kind in exclude_kinds if kind]
        clause = ""
        params: list[object] = [event_id]
        if excluded:
            clause = f" AND kind NOT IN ({', '.join('?' for _ in excluded)})"
            params.extend(excluded)
        with self.db.internal() as conn:
            total = conn.execute(
                f"SELECT count(*) FROM memory_events WHERE id > ?{clause}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT id, ts, kind, content FROM memory_events WHERE id > ?{clause} "
                "ORDER BY id DESC LIMIT ?",
                [*params, max_events],
            ).fetchall()
        rows.reverse()
        return rows, total

    def anchored(self, checkpoint: Checkpoint) -> bool:
        """Does the checkpoint still point at the event hash it was written against?"""
        if checkpoint.last_event_id == 0:
            return checkpoint.head_hash == GENESIS
        with self.db.internal() as conn:
            row = conn.execute(
                "SELECT hash FROM memory_events WHERE id = ?", (checkpoint.last_event_id,)
            ).fetchone()
        return bool(row) and row[0] == checkpoint.head_hash
