import pytest
from mcp.client import Client

from mcp_sqlite_memory.db import Database
from mcp_sqlite_memory.server import build_server
from mcp_sqlite_memory.settings import Settings


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        db_path=tmp_path / "test.db",
        query_timeout=2.0,
        default_limit=10,
        max_rows=50,
        max_result_bytes=4096,
        max_cell_chars=40,
        max_update_rows=5,
        search_candidates=100,
        snapshots=3,
    )


@pytest.fixture
def db(settings):
    return Database(settings)


@pytest.fixture
def seeded(db):
    with db.internal() as conn:
        conn.executescript(
            """
            CREATE TABLE notes(id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT, score REAL);
            INSERT INTO notes(title, body, score) VALUES
                ('alpha', 'first note about apples', 1.5),
                ('beta', 'second note, with "quotes" and, commas', 2.0),
                ('gamma', NULL, NULL);
            CREATE TABLE tags(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id INTEGER REFERENCES notes(id),
                tag TEXT
            );
            INSERT INTO tags(note_id, tag) VALUES (1, 'fruit'), (2, 'punctuation');
            CREATE INDEX tags_note ON tags(note_id);
            """
        )
    return db


@pytest.fixture
def server(settings, seeded):
    return build_server(settings)


@pytest.fixture
async def client(server):
    async with Client(server) as c:
        yield c
