"""Protocol-level tests: the server exactly as an MCP client sees it."""

import pytest

from mcp_sqlite_memory.search import find_ripgrep

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "read_query",
    "write_query",
    "destructive_query",
    "list_tables",
    "describe_table",
    "get_schema",
    "create_view",
    "append_event",
    "snapshot",
    "verify_chain",
    "checkpoint",
    "get_resume_context",
    "search_text",
}


def text(result) -> str:
    return "".join(b.text for b in result.content if getattr(b, "type", None) == "text")


async def call(client, tool, **args):
    return await client.call_tool(tool, args)


async def test_tool_surface(client, settings):
    tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == EXPECTED_TOOLS
    assert tools["read_query"].annotations.read_only_hint is True
    assert tools["destructive_query"].annotations.destructive_hint is True
    assert tools["write_query"].annotations.destructive_hint is False
    assert all(t.output_schema is None for t in tools.values())
    limit = tools["read_query"].input_schema["properties"]["limit"]
    assert limit["maximum"] == settings.max_rows and limit["default"] == settings.default_limit
    assert "reason" in tools["destructive_query"].input_schema["required"]


async def test_read_query_returns_csv(client):
    result = await call(client, "read_query", query="SELECT id, title FROM notes ORDER BY id")
    assert not result.is_error
    assert text(result) == "id,title\n1,alpha\n2,beta\n3,gamma\n# 3 rows"


async def test_read_query_quotes_and_nulls(client):
    result = await call(
        client, "read_query", query="SELECT body, score FROM notes WHERE id IN (2, 3) ORDER BY id"
    )
    lines = text(result).splitlines()
    assert lines[1] == '"second note, with ""quotes"" and, commas",2.0'
    assert lines[2] == ","


async def test_read_query_limit_trailer(client):
    result = await call(client, "read_query", query="SELECT id FROM notes ORDER BY id", limit=2)
    assert text(result).endswith(
        "# 2 rows, truncated by limit=2 (more rows exist; raise limit up to 50 or narrow the query)"
    )


async def test_read_query_limit_above_the_cap_is_rejected(client):
    result = await call(client, "read_query", query="SELECT 1", limit=10_000)
    assert result.is_error


async def test_sqlite_errors_reach_the_agent(client):
    result = await call(client, "read_query", query="SELECT * FROM nope")
    assert result.is_error and "no such table: nope" in text(result)


async def test_read_query_refuses_writes(client):
    result = await call(client, "read_query", query="DELETE FROM notes")
    assert result.is_error and "use destructive_query" in text(result)


async def test_write_query_insert(client):
    result = await call(client, "write_query", query="INSERT INTO notes(title) VALUES ('delta')")
    assert text(result) == "ok: 1 rows affected (last_insert_rowid=4)"
    count = await call(client, "read_query", query="SELECT count(*) FROM notes")
    assert text(count).startswith("count(*)\n4\n")


async def test_write_query_returning(client):
    result = await call(
        client, "write_query", query="INSERT INTO notes(title) VALUES ('eps') RETURNING id, title"
    )
    assert text(result).startswith("ok:") and "id,title\n4,eps" in text(result)


async def test_write_query_denies_drop(client):
    result = await call(client, "write_query", query="DROP TABLE notes")
    assert result.is_error
    assert "DROP TABLE is not allowed in write_query; use destructive_query" in text(result)


async def test_update_guard_through_the_tool(client):
    for i in range(6):
        await call(client, "write_query", query=f"INSERT INTO notes(title) VALUES ('n{i}')")
    result = await call(client, "write_query", query="UPDATE notes SET score = 0")
    assert result.is_error and "rolled back" in text(result)
    untouched = await call(client, "read_query", query="SELECT count(*) FROM notes WHERE score = 0")
    assert "\n0\n" in text(untouched)


async def test_destructive_query_snapshots_first(client, db):
    result = await call(
        client, "destructive_query", query="DELETE FROM notes WHERE id = 3", reason="test cleanup"
    )
    assert not result.is_error
    snapshots = db.list_snapshots()
    assert len(snapshots) == 1
    assert text(result) == f"ok: 1 rows affected; snapshot: {snapshots[0]}"


async def test_destructive_query_requires_a_reason(client, db):
    result = await call(client, "destructive_query", query="DELETE FROM notes", reason="  ")
    assert result.is_error and "reason" in text(result)
    assert db.list_snapshots() == []


async def test_destructive_preflight_costs_no_snapshot(client, db):
    result = await call(client, "destructive_query", query="DROP TABLE nope", reason="x")
    assert result.is_error and "no such table" in text(result)
    denied = await call(client, "destructive_query", query="DROP TABLE query_log", reason="x")
    assert denied.is_error and "managed by the server" in text(denied)
    assert db.list_snapshots() == []


async def test_list_tables(client):
    body = text(await call(client, "list_tables"))
    assert body.startswith("name,type,system,rows\n")
    assert "notes,table,0,3\n" in body
    assert "memory_events,table,1,0\n" in body
    assert "query_errors,view,1,\n" in body


async def test_describe_table(client):
    body = text(await call(client, "describe_table", table="tags"))
    assert "# columns\ncid,name,type,notnull,default,pk\n0,id,INTEGER,0,,1\n" in body
    assert "# foreign_keys\nfrom,to_table,to_column\nnote_id,notes,id\n" in body
    assert "# indexes\nname,unique,origin,columns\ntags_note,0,c,note_id" in body
    missing = await call(client, "describe_table", table="nope")
    assert missing.is_error and "no table or view" in text(missing)


async def test_get_schema(client):
    body = text(await call(client, "get_schema"))
    assert "CREATE TABLE notes(" in body and "memory_events" not in body
    assert "server objects hidden" in body
    full = text(await call(client, "get_schema", include_system=True))
    assert "CREATE TABLE memory_events" in full
    assert "-- description: failed tool calls" in full


async def test_create_view_roundtrip(client):
    created = await call(
        client,
        "create_view",
        name="recent",
        select_sql="SELECT id, title FROM notes ORDER BY id DESC;",
        description="newest  notes\nfirst",
    )
    assert text(created) == "ok: view recent created"
    schema = text(await call(client, "get_schema"))
    assert (
        'CREATE VIEW "recent" AS\n-- description: newest notes first\n'
        "SELECT id, title FROM notes ORDER BY id DESC;" in schema
    )
    rows = text(await call(client, "read_query", query="SELECT * FROM recent"))
    assert rows.startswith("id,title\n3,gamma\n")
    again = await call(client, "create_view", name="recent", select_sql="SELECT id FROM notes")
    assert again.is_error and "replace=true" in text(again)
    replaced = await call(
        client, "create_view", name="recent", select_sql="SELECT id FROM notes", replace=True
    )
    assert text(replaced).startswith("ok: view recent replaced")
    log = text(
        await call(
            client,
            "read_query",
            query="SELECT instr(detail, 'previous_sql') > 0, instr(detail, 'ORDER BY id DESC') > 0 "
            "FROM query_log WHERE tool = 'create_view' ORDER BY id DESC LIMIT 1",
        )
    )
    assert log.splitlines()[1] == "1,1"  # the audit row keeps the replaced definition


@pytest.mark.parametrize(
    "name,select_sql,needle",
    [
        ("bad name", "SELECT 1", "view name"),
        ("memory_events", "SELECT 1", "server table"),
        ("v", "DELETE FROM notes", "not allowed"),
        ("v", "INSERT INTO notes(title) VALUES ('x')", "not allowed"),
        ("notes", "SELECT 1", "already exists"),
    ],
)
async def test_create_view_rejections(client, name, select_sql, needle):
    result = await call(client, "create_view", name=name, select_sql=select_sql)
    assert result.is_error and needle in text(result)


async def test_memory_flow(client):
    first = text(await call(client, "append_event", content="chose sqlite", kind="decision"))
    assert first.startswith("ok: event 1 appended (hash ")
    await call(client, "append_event", content="loaded data")
    cp = text(await call(client, "checkpoint", summary="two events in, all good"))
    assert cp == "ok: checkpoint 1 written at event 2"
    await call(client, "append_event", content="after checkpoint")
    resume = text(await call(client, "get_resume_context"))
    assert resume.startswith("# checkpoint 1 | ")
    assert (
        "| at event 2\ntwo events in, all good\n\n"
        "# 1 events since checkpoint | chain ok (3 events)\nid,ts,kind,content\n3," in resume
    )
    assert resume.endswith(",note,after checkpoint")
    verify = text(await call(client, "verify_chain"))
    assert '"ok": true' in verify and '"events": 3' in verify


async def test_resume_without_a_checkpoint(client):
    resume = text(await call(client, "get_resume_context"))
    assert resume == "# no checkpoint yet\n\n# 0 events | chain ok (0 events)"


async def test_audit_log_records_calls_and_errors(client):
    await call(client, "read_query", query="SELECT 1")
    await call(client, "read_query", query="SELECT * FROM nope")
    log = text(
        await call(
            client, "read_query", query="SELECT tool, ok, rows, error FROM query_log ORDER BY id"
        )
    )
    assert "read_query,1,1,\n" in log
    assert "read_query,0,,SQLite OperationalError: no such table" in log  # cell cut at 40 chars
    errors = text(await call(client, "read_query", query="SELECT tool, error FROM query_errors"))
    assert "read_query,SQLite OperationalError: no such table" in errors


async def test_pragma_writes_are_denied(client):
    result = await call(client, "read_query", query="PRAGMA journal_mode = WAL")
    assert result.is_error and "PRAGMA journal_mode is not allowed" in text(result)
    ok = await call(client, "read_query", query="PRAGMA journal_mode")
    assert not ok.is_error


async def test_multiple_statements_are_rejected(client):
    result = await call(client, "read_query", query="SELECT 1; DROP TABLE notes")
    assert result.is_error and "one statement at a time" in text(result)


@pytest.mark.skipif(find_ripgrep(None) is None, reason="ripgrep is not installed")
async def test_search_text_tool(client):
    result = await call(client, "search_text", pattern="apple", table="notes")
    assert not result.is_error
    body = text(result)
    assert body.startswith("key,column,snippet\n1,body,")
    assert body.endswith(
        "# 1 of 1 matching values in 1 rows (3 candidate rows x 2 columns scanned)"
    )


async def test_resume_excludes_kinds_and_keeps_newest(client, db):
    await call(client, "append_event", content="curated one", kind="insight")
    for i in range(80):
        await call(client, "append_event", content=f"raw prompt {i} " + "x" * 300, kind="prompt")
    await call(client, "append_event", content="curated two", kind="decision")
    plain = text(await call(client, "get_resume_context", max_events=50))
    assert "older events dropped" in plain  # 50 newest rows of ~90 bytes overflow the 4096-byte cap
    assert ",decision,curated two\n# 3 older events dropped" in plain  # newest kept, oldest cut
    filtered = text(
        await call(client, "get_resume_context", max_events=50, exclude_kinds="prompt, compaction")
    )
    assert "# 2 events | chain ok (82 events)" in filtered
    assert "raw prompt" not in filtered
    assert ",insight,curated one" in filtered and ",decision,curated two" in filtered
