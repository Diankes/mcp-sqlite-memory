"""Connections, policy enforcement, timeouts, bootstrap and snapshots."""

import logging
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from mcp_sqlite_memory.errors import PolicyViolation, QueryTimeout, UpdateGuard
from mcp_sqlite_memory.guard import DDL_ACTIONS, Authorizer, Policy
from mcp_sqlite_memory.settings import Settings

log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 5.0  # wait this long for a lock held by another program before failing
PROGRESS_EVERY_N_OPS = 5_000  # how often SQLite asks the deadline whether to interrupt

BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS memory_events (
    id        INTEGER PRIMARY KEY,
    ts        TEXT    NOT NULL,
    session   TEXT    NOT NULL,
    kind      TEXT    NOT NULL DEFAULT 'note',
    content   TEXT    NOT NULL,
    prev_hash TEXT    NOT NULL,
    hash      TEXT    NOT NULL UNIQUE
);
CREATE TRIGGER IF NOT EXISTS memory_events_no_update BEFORE UPDATE ON memory_events
    BEGIN SELECT RAISE(ABORT, 'memory_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS memory_events_no_delete BEFORE DELETE ON memory_events
    BEGIN SELECT RAISE(ABORT, 'memory_events is append-only'); END;

CREATE TABLE IF NOT EXISTS memory_checkpoints (
    id            INTEGER PRIMARY KEY,
    ts            TEXT    NOT NULL,
    session       TEXT    NOT NULL,
    summary       TEXT    NOT NULL,
    last_event_id INTEGER NOT NULL,
    head_hash     TEXT    NOT NULL
);
CREATE TRIGGER IF NOT EXISTS memory_checkpoints_no_update BEFORE UPDATE ON memory_checkpoints
    BEGIN SELECT RAISE(ABORT, 'memory_checkpoints is append-only'); END;
CREATE TRIGGER IF NOT EXISTS memory_checkpoints_no_delete BEFORE DELETE ON memory_checkpoints
    BEGIN SELECT RAISE(ABORT, 'memory_checkpoints is append-only'); END;

CREATE TABLE IF NOT EXISTS query_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT    NOT NULL,
    session     TEXT    NOT NULL,
    tool        TEXT    NOT NULL,
    query       TEXT,
    ok          INTEGER NOT NULL,
    rows        INTEGER,
    error       TEXT,
    duration_ms INTEGER NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS query_log_ts ON query_log(ts);
CREATE TRIGGER IF NOT EXISTS query_log_no_update BEFORE UPDATE ON query_log
    BEGIN SELECT RAISE(ABORT, 'query_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS query_log_no_delete BEFORE DELETE ON query_log
    BEGIN SELECT RAISE(ABORT, 'query_log is append-only'); END;

CREATE VIEW IF NOT EXISTS query_errors AS
-- description: failed tool calls, newest first; shows where queries go wrong
SELECT id, ts, tool, query, error FROM query_log WHERE ok = 0 ORDER BY id DESC;
"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_session_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(2)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class Deadline:
    """Progress-handler callback: returns True (interrupt the statement) once time is up."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.expires_at = time.monotonic() + seconds
        self.tripped = False

    def check(self) -> bool:
        if time.monotonic() > self.expires_at:
            self.tripped = True
            return True
        return False


@dataclass
class ExecResult:
    columns: list[str]
    rows: list[tuple]
    rowcount: int
    lastrowid: int | None
    actions: set[int] = field(default_factory=set)


@dataclass
class AgentConn:
    """A policy-guarded connection for SQL written by the agent."""

    conn: sqlite3.Connection
    auth: Authorizer
    deadline: Deadline

    def query(self, sql: str, params: tuple = (), *, max_fetch: int | None = None) -> ExecResult:
        self.auth.reset()
        try:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall() if max_fetch is None else cur.fetchmany(max_fetch)
        except sqlite3.Error as exc:
            self._raise(exc)
        columns = [d[0] for d in cur.description] if cur.description else []
        return ExecResult(columns, rows, cur.rowcount, cur.lastrowid, set(self.auth.seen))

    def preflight(self, sql: str) -> set[int]:
        """Compile without executing: checks syntax and policy, returns the actions seen."""
        self.auth.reset()
        try:
            self.conn.execute("EXPLAIN " + sql).fetchall()
        except sqlite3.Error as exc:
            self._raise(exc)
        return set(self.auth.seen)

    def write(self, sql: str, *, max_update_rows: int | None = None) -> ExecResult:
        """Run one statement inside a transaction; roll back on any failure or guard."""
        with self.auth.suspended():
            self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.auth.reset()
            cur = self.conn.execute(sql)
            rows = cur.fetchall()  # RETURNING clauses produce rows
            seen = set(self.auth.seen)
            pure_update = sqlite3.SQLITE_UPDATE in seen and not (
                seen & ({sqlite3.SQLITE_INSERT, sqlite3.SQLITE_DELETE} | DDL_ACTIONS)
            )
            if max_update_rows is not None and pure_update and cur.rowcount > max_update_rows:
                raise UpdateGuard(
                    f"UPDATE would change {cur.rowcount} rows, above the write_query cap of "
                    f"{max_update_rows}; rolled back. Add a WHERE clause, or use "
                    "destructive_query if you really mean every row."
                )
            with self.auth.suspended():
                self.conn.execute("COMMIT")
        except BaseException as exc:
            with self.auth.suspended():
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
            if isinstance(exc, sqlite3.Error):
                self._raise(exc)
            raise
        columns = [d[0] for d in cur.description] if cur.description else []
        return ExecResult(columns, rows, cur.rowcount, cur.lastrowid, seen)

    def _raise(self, exc: sqlite3.Error) -> NoReturn:
        message = str(exc)
        if self.deadline.tripped and "interrupted" in message:
            raise QueryTimeout(
                f"query exceeded {self.deadline.seconds:g}s and was cancelled; "
                "narrow it, add a LIMIT, or add an index"
            ) from exc
        if self.auth.denied is not None and "not authorized" in message:
            raise PolicyViolation(self.auth.denied) from exc
        raise exc


class Database:
    """One SQLite file: bootstrap, connections, snapshots."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path = settings.db_path.expanduser().resolve()
        self.session = new_session_id()
        self._default_snapshot_dir = self.path.parent / f"{self.path.stem}.snapshots"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    @property
    def snapshot_dir(self) -> Path:
        """Where snapshots go: --snapshot-dir when set, else <db>.snapshots/ next to the file."""
        if self.settings.snapshot_dir is not None:
            return self.settings.snapshot_dir.expanduser()
        return self._default_snapshot_dir

    def _raw(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)

    @contextmanager
    def internal(self) -> Iterator[sqlite3.Connection]:
        """Server-owned connection with no policy: bootstrap, memory and audit writes."""
        with closing(self._raw()) as conn:
            yield conn

    @contextmanager
    def agent(self, policy: Policy) -> Iterator[AgentConn]:
        """Connection for agent SQL: defensive mode, read-only pragma, authorizer, deadline."""
        with closing(self._raw()) as conn:
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
            if policy.read_only:
                conn.execute("PRAGMA query_only = 1")
            auth = Authorizer(policy, self.virtual_tables(conn))
            deadline = Deadline(self.settings.query_timeout)
            conn.set_authorizer(auth)
            conn.set_progress_handler(deadline.check, PROGRESS_EVERY_N_OPS)
            yield AgentConn(conn, auth, deadline)

    @staticmethod
    def virtual_tables(conn: sqlite3.Connection) -> frozenset[str]:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND sql LIKE 'CREATE VIRTUAL TABLE%'"
        )
        return frozenset(row[0] for row in rows)

    def snapshot(self) -> Path | None:
        """Copy the database with the online backup API; keep the newest N copies.

        Returns None when snapshots are disabled or the target directory cannot be used
        (a removable drive that is not plugged in, a file where the directory should be).
        A snapshot problem is logged and never fails the caller."""
        if self.settings.snapshots <= 0:
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%f")[:-3] + "Z"
        target = self.snapshot_dir / f"{self.path.stem}.{stamp}.db"
        try:
            self.snapshot_dir.mkdir(parents=True, exist_ok=True)
            with self.internal() as src, closing(sqlite3.connect(target)) as dst:
                src.backup(dst)
            self._rotate()
        except (OSError, sqlite3.Error) as exc:
            with suppress(OSError):
                target.unlink(missing_ok=True)  # never leave a half-written copy behind
            log.warning("snapshot skipped: %s", exc)
            return None
        log.info("snapshot written: %s", target)
        return target

    def list_snapshots(self) -> list[Path]:
        if not self.snapshot_dir.is_dir():
            return []
        return sorted(self.snapshot_dir.glob(f"{self.path.stem}.*.db"))

    def _rotate(self) -> None:
        for old in self.list_snapshots()[: -self.settings.snapshots]:
            old.unlink(missing_ok=True)
            log.info("snapshot rotated out: %s", old)

    def _bootstrap(self) -> None:
        with self.internal() as conn:
            try:
                conn.execute("PRAGMA schema_version").fetchone()
            except sqlite3.DatabaseError as exc:
                raise SystemExit(f"{self.path} is not a SQLite database: {exc}") from exc
            conn.executescript(BOOTSTRAP_SQL)
