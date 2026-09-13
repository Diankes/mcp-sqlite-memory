import sqlite3

import pytest
from mcp.client import Client

from mcp_sqlite_memory.db import Database
from mcp_sqlite_memory.server import build_server
from mcp_sqlite_memory.settings import Settings


def text(result) -> str:
    return "".join(b.text for b in result.content if getattr(b, "type", None) == "text")


def test_snapshots_rotate_to_the_configured_count(db):
    written = [db.snapshot() for _ in range(5)]
    kept = db.list_snapshots()
    assert len(kept) == 3
    assert kept == sorted(written[-3:])
    assert all(p.parent == db.snapshot_dir for p in kept)


def test_snapshot_is_a_consistent_copy(seeded):
    path = seeded.snapshot()
    with sqlite3.connect(path) as copy:
        assert copy.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
        assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_snapshots_can_be_disabled(tmp_path):
    db = Database(Settings(db_path=tmp_path / "x.db", snapshots=0))
    assert db.snapshot() is None
    assert not db.snapshot_dir.exists()


def test_snapshot_dir_override_replaces_the_default_location(tmp_path):
    target = tmp_path / "usb" / "backups"
    db = Database(Settings(db_path=tmp_path / "x.db", snapshot_dir=target, snapshots=2))
    assert db.snapshot_dir == target
    written = [db.snapshot() for _ in range(3)]
    assert all(p is not None and p.parent == target for p in written)
    assert all(p.exists() for p in written[-2:]) and not written[0].exists()  # rotated
    assert db.list_snapshots() == sorted(written[-2:])
    assert not (tmp_path / "x.snapshots").exists()


def test_unreachable_snapshot_dir_is_skipped_not_raised(tmp_path, caplog):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("a file sits where the snapshot directory should be")
    db = Database(Settings(db_path=tmp_path / "x.db", snapshot_dir=blocker))
    with caplog.at_level("WARNING"):
        assert db.snapshot() is None
    assert "snapshot skipped" in caplog.text
    assert db.list_snapshots() == []


@pytest.mark.anyio
async def test_snapshot_tool_writes_and_audits(seeded, settings):
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("snapshot", {"reason": "before the seminar rewrite"})
        assert not result.is_error
        [path] = seeded.list_snapshots()
        assert text(result) == f"ok: snapshot written: {path}"
        with sqlite3.connect(path) as copy:
            assert copy.execute("SELECT count(*) FROM notes").fetchone()[0] == 3
        log = await client.call_tool(
            "read_query", {"query": "SELECT tool, query, ok FROM query_log WHERE tool = 'snapshot'"}
        )
        assert "snapshot,before the seminar rewrite,1" in text(log)
        default = await client.call_tool("snapshot", {})
        assert text(default).startswith("ok: snapshot written: ")
        assert len(seeded.list_snapshots()) == 2


@pytest.mark.anyio
async def test_snapshot_tool_reports_skips_without_failing(tmp_path):
    disabled = build_server(Settings(db_path=tmp_path / "off.db", snapshots=0))
    async with Client(disabled) as client:
        result = await client.call_tool("snapshot", {})
        assert not result.is_error
        assert text(result) == "ok: snapshot skipped (disabled or target unavailable)"
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    unreachable = build_server(Settings(db_path=tmp_path / "usb.db", snapshot_dir=blocker))
    async with Client(unreachable) as client:
        result = await client.call_tool("snapshot", {"reason": "usb unplugged"})
        assert not result.is_error
        assert text(result) == "ok: snapshot skipped (disabled or target unavailable)"
        log = await client.call_tool(
            "read_query", {"query": "SELECT ok, detail FROM query_log WHERE tool = 'snapshot'"}
        )
        assert '""path"": null' in text(log)
