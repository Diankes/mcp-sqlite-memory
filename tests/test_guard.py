"""The statement policy, exercised straight through the guarded connection."""

import sqlite3

import pytest

from mcp_sqlite_memory.errors import PolicyViolation, QueryTimeout, UpdateGuard
from mcp_sqlite_memory.guard import DESTRUCTIVE, READ, WRITE


def run(db, policy, sql, **kwargs):
    with db.agent(policy) as ac:
        if policy.read_only:
            return ac.query(sql)
        return ac.write(sql, **kwargs)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM notes",
        "WITH x AS (SELECT 1 AS a) SELECT a FROM x",
        "/* c */ -- c\nSELECT 1",
        "EXPLAIN QUERY PLAN SELECT * FROM notes",
        "PRAGMA table_info(notes)",
        "PRAGMA foreign_key_list(tags)",
        "PRAGMA journal_mode",
        "SELECT * FROM pragma_table_info('notes')",
        "SELECT * FROM memory_events",
        "SELECT * FROM query_errors",
    ],
)
def test_read_policy_allows_reads(seeded, sql):
    run(seeded, READ, sql)


@pytest.mark.parametrize(
    "sql,needle",
    [
        ("INSERT INTO notes(title) VALUES ('x')", "use write_query"),
        ("UPDATE notes SET title = 'x'", "use write_query"),
        ("DELETE FROM notes", "use destructive_query"),
        ("DROP TABLE notes", "use destructive_query"),
        ("WITH d AS (SELECT 1) INSERT INTO notes(title) SELECT 'x' FROM d", "use write_query"),
        ("CREATE TABLE t(a)", "use write_query"),
        ("PRAGMA journal_mode = WAL", "PRAGMA journal_mode is not allowed"),
        ("PRAGMA writable_schema = 1", "PRAGMA writable_schema is not allowed"),
        ("ATTACH DATABASE ':memory:' AS other", "ATTACH is not allowed"),
        ("BEGIN", "transaction control"),
        ("/* sneaky */ DROP TABLE notes", "use destructive_query"),
        ("SELECT load_extension('x')", "load_extension"),
    ],
)
def test_read_policy_denies_everything_else(seeded, sql, needle):
    with pytest.raises(PolicyViolation, match=needle):
        run(seeded, READ, sql)


def test_multiple_statements_are_rejected(seeded):
    with pytest.raises(sqlite3.ProgrammingError, match="one statement at a time"):
        run(seeded, READ, "SELECT 1; DROP TABLE notes")


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO notes(title) VALUES ('x')",
        "INSERT INTO tags(note_id, tag) VALUES (1, 'autoincrement bookkeeping')",
        "UPDATE notes SET score = 3 WHERE id = 1",
        "INSERT INTO notes(id, title) VALUES (1, 'dup') "
        "ON CONFLICT(id) DO UPDATE SET title = excluded.title",
        "REPLACE INTO notes(id, title) VALUES (2, 'replaced')",
        "CREATE TABLE t3(a)",
        "CREATE TABLE t4 AS SELECT id FROM notes",
        "CREATE INDEX notes_title ON notes(title)",
        "ANALYZE",
    ],
)
def test_write_policy_allows_dml_and_additive_ddl(seeded, sql):
    run(seeded, WRITE, sql)


@pytest.mark.parametrize(
    "sql,needle",
    [
        ("DELETE FROM notes WHERE id = 1", "use destructive_query"),
        ("DROP TABLE tags", "use destructive_query"),
        ("DROP INDEX tags_note", "use destructive_query"),
        ("ALTER TABLE notes ADD COLUMN extra TEXT", "use destructive_query"),
        ("ALTER TABLE notes RENAME TO notes2", "use destructive_query"),
        (
            "CREATE TRIGGER tr AFTER INSERT ON notes BEGIN DELETE FROM tags; END",
            "destructive_query",
        ),
        ("CREATE VIEW v AS SELECT 1", "create_view"),
        ("CREATE TEMP TABLE tt(a)", "temporary objects"),
        ("ATTACH DATABASE ':memory:' AS other", "ATTACH is not allowed"),
        ("PRAGMA journal_mode = WAL", "PRAGMA"),
        ("SAVEPOINT sp", "transaction control"),
    ],
)
def test_write_policy_denies_destructive(seeded, sql, needle):
    with pytest.raises(PolicyViolation, match=needle):
        run(seeded, WRITE, sql)


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM notes WHERE id = 1",
        "DELETE FROM notes",
        "DROP TABLE tags",
        "DROP INDEX tags_note",
        "ALTER TABLE notes ADD COLUMN extra TEXT",
        "ALTER TABLE notes RENAME TO notes2",
        "CREATE TRIGGER tr AFTER INSERT ON notes BEGIN SELECT 1; END",
        "UPDATE notes SET score = 0",
    ],
)
def test_destructive_policy_allows_destructive(seeded, sql):
    run(seeded, DESTRUCTIVE, sql)


@pytest.mark.parametrize("policy", [READ, WRITE, DESTRUCTIVE], ids=lambda p: p.tool)
@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM memory_events",
        "UPDATE memory_events SET content = 'x'",
        "INSERT INTO memory_events(id, ts, session, kind, content, prev_hash, hash) "
        "VALUES (99, 't', 's', 'k', 'c', 'p', 'h')",
        "DROP TABLE memory_events",
        "ALTER TABLE memory_events RENAME TO x",
        "ALTER TABLE query_log ADD COLUMN extra TEXT",
        "DROP TRIGGER memory_events_no_delete",
        "DROP INDEX query_log_ts",
        "CREATE TRIGGER t2 AFTER INSERT ON memory_checkpoints BEGIN SELECT 1; END",
        "CREATE INDEX evil ON memory_events(kind)",
        "DELETE FROM query_log",
        "UPDATE memory_checkpoints SET summary = 'x'",
    ],
)
def test_system_tables_are_protected_under_every_policy(seeded, policy, sql):
    with pytest.raises(PolicyViolation, match="managed by the server"):
        run(seeded, policy, sql)


def test_append_only_triggers_hold_even_without_the_authorizer(seeded):
    with seeded.internal() as conn:
        conn.execute(
            "INSERT INTO memory_events(id, ts, session, kind, content, prev_hash, hash) "
            "VALUES (1, 't', 's', 'k', 'c', 'p', 'h')"
        )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE memory_events SET content = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM memory_events")


def test_query_only_is_a_backstop_behind_the_authorizer(seeded):
    with (
        seeded.agent(READ) as ac,
        ac.auth.suspended(),
        pytest.raises(sqlite3.OperationalError, match="readonly"),
    ):
        ac.conn.execute("INSERT INTO notes(title) VALUES ('x')")


def test_wall_clock_timeout_interrupts_runaway_queries(seeded):
    with pytest.raises(QueryTimeout, match="exceeded 2s"):
        run(
            seeded,
            READ,
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
            "SELECT count(*) FROM c",
        )


def test_update_guard_rolls_back_wide_updates(seeded):
    for i in range(6):
        run(seeded, WRITE, f"INSERT INTO notes(title, score) VALUES ('n{i}', 1)")
    with pytest.raises(UpdateGuard, match="rolled back"):
        run(seeded, WRITE, "UPDATE notes SET score = 0", max_update_rows=5)
    assert run(seeded, READ, "SELECT count(*) FROM notes WHERE score = 0").rows[0][0] == 0
    run(seeded, WRITE, "UPDATE notes SET score = 0 WHERE id = 1", max_update_rows=5)
    run(seeded, DESTRUCTIVE, "UPDATE notes SET score = 0")
    assert run(seeded, READ, "SELECT count(*) FROM notes WHERE score = 0").rows[0][0] == 9


def test_preflight_checks_policy_without_running(seeded):
    with seeded.agent(WRITE) as ac:
        with pytest.raises(PolicyViolation, match="use destructive_query"):
            ac.preflight("DELETE FROM notes")
        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            ac.preflight("DELETE FROM nope")
    with seeded.agent(DESTRUCTIVE) as ac:
        ac.preflight("DELETE FROM notes")
    assert run(seeded, READ, "SELECT count(*) FROM notes").rows[0][0] == 3


def test_fts5_tables_work_under_the_write_policy(seeded):
    run(seeded, WRITE, "CREATE VIRTUAL TABLE docs USING fts5(body)")
    with seeded.agent(WRITE) as ac:
        for i in range(60):
            ac.write(f"INSERT INTO docs(body) VALUES ('document {i} about apples')")
        ac.write("INSERT INTO docs(docs) VALUES ('optimize')")
    hits = run(seeded, READ, "SELECT count(*) FROM docs WHERE docs MATCH 'apples'").rows[0][0]
    assert hits == 60
    with pytest.raises(PolicyViolation, match="module"):
        run(seeded, WRITE, "CREATE VIRTUAL TABLE bad USING nosuchmodule(a)")
