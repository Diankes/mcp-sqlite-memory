import sqlite3

from mcp_sqlite_memory.db import Database
from mcp_sqlite_memory.settings import Settings


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
