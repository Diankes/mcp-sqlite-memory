import pytest

from mcp_sqlite_memory.errors import UserError
from mcp_sqlite_memory.memory import GENESIS, Memory, event_hash


def test_chain_grows_and_verifies(db):
    memory = Memory(db)
    ids = [memory.append(f"event {i}", "note")[0] for i in range(3)]
    assert ids == [1, 2, 3]
    status = memory.verify()
    assert status.ok and status.events == 3 and status.head_id == 3
    with db.internal() as conn:
        first_prev, first_hash, second_prev = conn.execute(
            "SELECT e1.prev_hash, e1.hash, e2.prev_hash FROM memory_events e1, memory_events e2 "
            "WHERE e1.id = 1 AND e2.id = 2"
        ).fetchone()
        last_hash = conn.execute("SELECT hash FROM memory_events WHERE id = 3").fetchone()[0]
    assert first_prev == GENESIS and second_prev == first_hash
    assert status.head_hash == last_hash


def test_hash_covers_every_field():
    base = event_hash(1, "t", "s", "note", "c", GENESIS)
    assert base == event_hash(1, "t", "s", "note", "c", GENESIS)
    assert base != event_hash(2, "t", "s", "note", "c", GENESIS)
    assert base != event_hash(1, "t", "s", "decision", "c", GENESIS)
    assert base != event_hash(1, "t", "s", "note", "c!", GENESIS)


def test_edited_content_is_detected(db):
    memory = Memory(db)
    for i in range(3):
        memory.append(f"event {i}")
    with db.internal() as conn:
        conn.execute("DROP TRIGGER memory_events_no_update")
        conn.execute("UPDATE memory_events SET content = 'forged' WHERE id = 2")
    status = memory.verify()
    assert not status.ok and status.break_at_id == 2 and "recomputed" in status.reason
    assert '"break_at_id": 2' in status.to_json()


def test_deleted_row_is_detected(db):
    memory = Memory(db)
    for i in range(3):
        memory.append(f"event {i}")
    with db.internal() as conn:
        conn.execute("DROP TRIGGER memory_events_no_delete")
        conn.execute("DELETE FROM memory_events WHERE id = 2")
    status = memory.verify()
    assert not status.ok and status.break_at_id == 3 and "gap" in status.reason
    assert status.events == 2


def test_checkpoint_anchors_to_the_chain(db):
    memory = Memory(db)
    memory.append("one")
    memory.append("two")
    cp = memory.checkpoint("halfway")
    assert cp.id == 1 and cp.last_event_id == 2
    memory.append("three")
    assert memory.latest_checkpoint() == cp
    events, total = memory.events_after(cp.last_event_id, 10)
    assert total == 1 and [e[0] for e in events] == [3]
    assert memory.anchored(cp)
    events, total = memory.events_after(0, 2)
    assert total == 3 and [e[0] for e in events] == [2, 3]  # most recent two, oldest first


def test_empty_input_is_rejected(db):
    memory = Memory(db)
    with pytest.raises(UserError):
        memory.append("   ")
    with pytest.raises(UserError):
        memory.checkpoint("")
    assert memory.verify().ok and memory.verify().events == 0
