"""Statement policy, enforced through SQLite's authorizer callback.

SQLite calls the authorizer while it *compiles* a statement, once for every
action the statement would perform (read a column, insert into a table, drop a
view, ...). Deciding there, rather than by inspecting SQL text, means comments,
CTEs, casing, whitespace and clever phrasing cannot disguise what a statement
does. Python's sqlite3 module adds one more rail for free: execute() refuses
strings that contain more than one statement.
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

S = sqlite3

SYSTEM_TABLES = frozenset({"memory_events", "memory_checkpoints", "query_log"})
SYSTEM_VIEWS = frozenset({"query_errors"})
SYSTEM_OBJECTS = SYSTEM_TABLES | SYSTEM_VIEWS

VTAB_MODULES = frozenset({"fts5", "fts4", "fts3", "rtree"})
_SHADOW_SUFFIXES = frozenset(
    {
        "data",
        "idx",
        "content",
        "docsize",
        "config",
        "segments",
        "segdir",
        "stat",
        "node",
        "rowid",
        "parent",
    }
)
_SCHEMA_TABLES = frozenset(
    {"sqlite_master", "sqlite_temp_master", "sqlite_schema", "sqlite_temp_schema"}
)

_ALWAYS = frozenset({S.SQLITE_SELECT, S.SQLITE_READ, S.SQLITE_FUNCTION, S.SQLITE_RECURSIVE})
_DML = frozenset({S.SQLITE_INSERT, S.SQLITE_UPDATE, S.SQLITE_DELETE})
_ADDITIVE_DDL = frozenset(
    {S.SQLITE_CREATE_TABLE, S.SQLITE_CREATE_INDEX, S.SQLITE_REINDEX, S.SQLITE_ANALYZE}
)
_DESTRUCTIVE_DDL = frozenset(
    {
        S.SQLITE_DROP_TABLE,
        S.SQLITE_DROP_VIEW,
        S.SQLITE_DROP_INDEX,
        S.SQLITE_DROP_TRIGGER,
        S.SQLITE_DROP_VTABLE,
        S.SQLITE_ALTER_TABLE,
        S.SQLITE_CREATE_TRIGGER,
    }
)
_TEMP = frozenset(
    {
        S.SQLITE_CREATE_TEMP_INDEX,
        S.SQLITE_CREATE_TEMP_TABLE,
        S.SQLITE_CREATE_TEMP_TRIGGER,
        S.SQLITE_CREATE_TEMP_VIEW,
        S.SQLITE_DROP_TEMP_INDEX,
        S.SQLITE_DROP_TEMP_TABLE,
        S.SQLITE_DROP_TEMP_TRIGGER,
        S.SQLITE_DROP_TEMP_VIEW,
    }
)
# Actions whose *second* argument names the table (see the sqlite3_set_authorizer docs).
_TABLE_IN_ARG2 = frozenset(
    {
        S.SQLITE_CREATE_INDEX,
        S.SQLITE_CREATE_TRIGGER,
        S.SQLITE_DROP_INDEX,
        S.SQLITE_DROP_TRIGGER,
        S.SQLITE_ALTER_TABLE,
    }
)
DDL_ACTIONS = _ADDITIVE_DDL | _DESTRUCTIVE_DDL | {S.SQLITE_CREATE_VIEW, S.SQLITE_CREATE_VTABLE}

ACTION_LABELS: dict[int, str] = {
    S.SQLITE_INSERT: "INSERT",
    S.SQLITE_UPDATE: "UPDATE",
    S.SQLITE_DELETE: "DELETE",
    S.SQLITE_CREATE_TABLE: "CREATE TABLE",
    S.SQLITE_CREATE_INDEX: "CREATE INDEX",
    S.SQLITE_CREATE_VIEW: "CREATE VIEW",
    S.SQLITE_CREATE_TRIGGER: "CREATE TRIGGER",
    S.SQLITE_CREATE_VTABLE: "CREATE VIRTUAL TABLE",
    S.SQLITE_DROP_TABLE: "DROP TABLE",
    S.SQLITE_DROP_VIEW: "DROP VIEW",
    S.SQLITE_DROP_INDEX: "DROP INDEX",
    S.SQLITE_DROP_TRIGGER: "DROP TRIGGER",
    S.SQLITE_DROP_VTABLE: "DROP VIRTUAL TABLE",
    S.SQLITE_ALTER_TABLE: "ALTER TABLE",
    S.SQLITE_REINDEX: "REINDEX",
    S.SQLITE_ANALYZE: "ANALYZE",
    S.SQLITE_ATTACH: "ATTACH",
    S.SQLITE_DETACH: "DETACH",
    S.SQLITE_TRANSACTION: "transaction control",
    S.SQLITE_SAVEPOINT: "SAVEPOINT",
    S.SQLITE_PRAGMA: "PRAGMA",
}

# PRAGMAs that only describe the schema; their argument is a name, never a setting.
INTROSPECTION_PRAGMAS = frozenset(
    {
        "table_info",
        "table_xinfo",
        "table_list",
        "index_list",
        "index_info",
        "index_xinfo",
        "foreign_key_list",
        "foreign_key_check",
        "function_list",
        "collation_list",
        "database_list",
        "compile_options",
        "integrity_check",
        "quick_check",
    }
)
# PRAGMAs that read a setting when given no value (and would change it when given one).
READ_STATE_PRAGMAS = frozenset(
    {
        "journal_mode",
        "user_version",
        "schema_version",
        "application_id",
        "page_count",
        "page_size",
        "freelist_count",
        "data_version",
        "encoding",
        "foreign_keys",
        "synchronous",
        "cache_size",
        "busy_timeout",
        "query_only",
        "auto_vacuum",
        "max_page_count",
        "mmap_size",
        "temp_store",
        "wal_autocheckpoint",
    }
)


def label(action: int) -> str:
    return ACTION_LABELS.get(action, f"action {action}")


def is_shadow_table(name: str, virtual_tables: frozenset[str]) -> bool:
    """True for the internal tables SQLite keeps behind an FTS or R*Tree table."""
    for vtab in virtual_tables:
        prefix = vtab + "_"
        if name.startswith(prefix) and name[len(prefix) :] in _SHADOW_SUFFIXES:
            return True
    return False


def _pragma_rule(name: str, value: str | None) -> str | None:
    lowered = name.lower()
    if lowered in INTROSPECTION_PRAGMAS:
        return None
    if lowered in READ_STATE_PRAGMAS and value is None:
        return None
    return (
        f"PRAGMA {name} is not allowed; only introspection pragmas (table_info, index_list, "
        "foreign_key_list, ...) and value-less reads such as PRAGMA journal_mode are"
    )


@dataclass(frozen=True)
class Policy:
    """What one tool may do. decide() returns None to allow, or the reason to refuse."""

    tool: str
    read_only: bool = True
    dml: bool = False
    additive_ddl: bool = False
    destructive: bool = False

    def decide(
        self, action: int, arg1: str | None, arg2: str | None, virtual_tables: frozenset[str]
    ) -> str | None:
        if action in _ALWAYS:
            if action == S.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
                return "load_extension() is not allowed"
            return None
        if action == S.SQLITE_PRAGMA:
            return _pragma_rule(arg1 or "", arg2)
        if action in _DML:
            return self._dml(action, arg1 or "", virtual_tables)
        if action == S.SQLITE_CREATE_VIEW:
            return (
                "CREATE VIEW is not allowed here; use the create_view tool, "
                "which also stores a description"
            )
        if action in _ADDITIVE_DDL:
            if action in (S.SQLITE_CREATE_TABLE, S.SQLITE_CREATE_INDEX):
                target = arg2 if action == S.SQLITE_CREATE_INDEX else arg1
                if target in SYSTEM_TABLES:
                    return self._managed(target)
            # ANALYZE and REINDEX only refresh statistics and indexes; they touch every
            # table, the server's included, without changing any row.
            if self.additive_ddl:
                return None
            return f"{label(action)} is not allowed in {self.tool}; use write_query"
        if action == S.SQLITE_CREATE_VTABLE:
            if not self.additive_ddl:
                return f"CREATE VIRTUAL TABLE is not allowed in {self.tool}; use write_query"
            if (arg2 or "").lower() not in VTAB_MODULES:
                allowed = ", ".join(sorted(VTAB_MODULES))
                return f"virtual table module {arg2!r} is not allowed (allowed: {allowed})"
            return None
        if action in _DESTRUCTIVE_DDL:
            target = arg2 if action in _TABLE_IN_ARG2 else arg1
            if target in SYSTEM_TABLES:
                return self._managed(target)
            if self.destructive:
                return None
            return (
                f"{label(action)} is not allowed in {self.tool}; use destructive_query "
                "(it snapshots the database first)"
            )
        if action in (S.SQLITE_ATTACH, S.SQLITE_DETACH):
            return f"{label(action)} is not allowed"
        if action in (S.SQLITE_TRANSACTION, S.SQLITE_SAVEPOINT):
            return "transaction control is handled by the server; send one statement at a time"
        if action in _TEMP:
            return "temporary objects are not allowed; every call runs on a fresh connection"
        return f"{label(action)} is not allowed"

    def _dml(self, action: int, table: str, virtual_tables: frozenset[str]) -> str | None:
        if table in SYSTEM_TABLES:
            return self._managed(table)
        if table in _SCHEMA_TABLES:
            # Bookkeeping writes that DDL makes to sqlite_master. Direct edits are
            # impossible anyway: they need PRAGMA writable_schema, which is denied.
            return None
        if action == S.SQLITE_DELETE:
            if self.destructive:
                return None
            if self.dml and is_shadow_table(table, virtual_tables):
                return None  # FTS / R*Tree maintenance deletes from its own shadow tables
            return (
                f"DELETE is not allowed in {self.tool}; use destructive_query "
                "(it snapshots the database first)"
            )
        if self.dml:
            return None
        return f"{label(action)} is not allowed in {self.tool}; use write_query"

    @staticmethod
    def _managed(table: str | None) -> str:
        return (
            f"{table} is managed by the server and is append-only; "
            "it cannot be changed through SQL tools"
        )


READ = Policy("read_query")
WRITE = Policy("write_query", read_only=False, dml=True, additive_ddl=True)
DESTRUCTIVE = Policy(
    "destructive_query", read_only=False, dml=True, additive_ddl=True, destructive=True
)


class Authorizer:
    """Per-connection callback: applies a Policy, records what it saw and why it first refused."""

    def __init__(self, policy: Policy, virtual_tables: frozenset[str]) -> None:
        self.policy = policy
        self.virtual_tables = virtual_tables
        self.seen: set[int] = set()
        self.denied: str | None = None
        self._suspended = False

    def __call__(
        self,
        action: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger_or_view: str | None,
    ) -> int:
        if self._suspended:
            return S.SQLITE_OK
        self.seen.add(action)
        reason = self.policy.decide(action, arg1, arg2, self.virtual_tables)
        if reason is None:
            return S.SQLITE_OK
        if self.denied is None:
            self.denied = reason
        return S.SQLITE_DENY

    def reset(self) -> None:
        self.seen.clear()
        self.denied = None

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Let the server's own BEGIN / COMMIT / ROLLBACK through."""
        self._suspended = True
        try:
            yield
        finally:
            self._suspended = False
