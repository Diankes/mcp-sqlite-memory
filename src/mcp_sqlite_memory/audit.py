"""Automatic audit log: every tool call lands in query_log, success or failure."""

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from mcp_sqlite_memory.db import Database, utc_now
from mcp_sqlite_memory.errors import ToolFailure

log = logging.getLogger(__name__)


@dataclass
class Span:
    tool: str
    query: str | None = None
    rows: int | None = None
    detail: dict[str, Any] | None = None
    error: str | None = None
    started: float = field(default_factory=time.perf_counter)


class Audit:
    def __init__(self, db: Database) -> None:
        self.db = db

    @contextmanager
    def span(self, tool: str, query: str | None = None) -> Iterator[Span]:
        """Wrap a tool body: log the call, and turn failures into ToolError so the message
        reaches the agent verbatim instead of the SDK's sanitized 'Error executing tool'."""
        span = Span(tool, query)
        try:
            yield span
        except ToolError as exc:
            span.error = str(exc)
            raise
        except ToolFailure as exc:
            span.error = str(exc)
            raise ToolError(str(exc)) from exc
        except sqlite3.Error as exc:
            span.error = f"SQLite {type(exc).__name__}: {exc}"
            raise ToolError(span.error) from exc
        except Exception as exc:
            span.error = f"{type(exc).__name__}: {exc}"
            log.exception("unexpected error in %s", tool)
            raise ToolError(span.error) from exc
        finally:
            self.record(span)

    def record(self, span: Span) -> None:
        duration_ms = int((time.perf_counter() - span.started) * 1000)
        detail = json.dumps(span.detail, ensure_ascii=False) if span.detail else None
        try:
            with self.db.internal() as conn:
                conn.execute(
                    "INSERT INTO query_log "
                    "(ts, session, tool, query, ok, rows, error, duration_ms, detail) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        utc_now(),
                        self.db.session,
                        span.tool,
                        span.query,
                        0 if span.error else 1,
                        span.rows,
                        span.error,
                        duration_ms,
                        detail,
                    ),
                )
        except sqlite3.Error as exc:
            log.warning("could not write the query_log entry for %s: %s", span.tool, exc)
