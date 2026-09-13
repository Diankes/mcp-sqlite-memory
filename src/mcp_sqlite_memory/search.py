"""search_text: SQL narrows the rows, ripgrep refines the text, in one call."""

import json
import os
import shutil
import subprocess
import tempfile
from contextlib import suppress
from dataclasses import dataclass

from mcp_sqlite_memory.csvfmt import render_table
from mcp_sqlite_memory.db import Database, quote_ident
from mcp_sqlite_memory.errors import QueryTimeout, UserError
from mcp_sqlite_memory.guard import READ
from mcp_sqlite_memory.settings import Settings

_TEXT_TYPE_HINTS = ("CHAR", "TEXT", "CLOB")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def find_ripgrep(explicit: str | None) -> str | None:
    return shutil.which(explicit or "rg")


@dataclass
class SearchOutcome:
    text: str
    sql: str
    columns: list[str]
    candidates: int
    capped: bool
    matches: int
    rows_matched: int


def _is_texty(declared_type: str | None) -> bool:
    if not declared_type:
        return True  # views and untyped columns: assume text
    upper = declared_type.upper()
    return any(hint in upper for hint in _TEXT_TYPE_HINTS)


def search_text(
    db: Database,
    settings: Settings,
    rg: str | None,
    *,
    pattern: str,
    table: str,
    columns: list[str] | None,
    where: str | None,
    key_column: str | None,
    limit: int,
    ignore_case: bool,
    fixed_strings: bool,
    context_chars: int,
) -> SearchOutcome:
    if rg is None:
        raise UserError(
            "ripgrep (rg) was not found on PATH; install it or start the server with --rg PATH"
        )
    if not pattern:
        raise UserError("pattern must not be empty")

    with db.agent(READ) as ac:
        info = ac.query("SELECT name, type FROM pragma_table_info(?)", (table,)).rows
        if not info:
            raise UserError(f"no table or view named {table!r}; use list_tables")
        kind_rows = ac.query("SELECT type FROM sqlite_master WHERE name = ?", (table,)).rows
        kind = kind_rows[0][0] if kind_rows else "table"
        available = [name for name, _ in info]
        if columns:
            missing = [c for c in columns if c not in available]
            if missing:
                raise UserError(f"unknown column(s) {missing} in {table}; available: {available}")
            searched = list(dict.fromkeys(columns))
        else:
            searched = [name for name, declared in info if _is_texty(declared)]
            if not searched:
                raise UserError(f"{table} has no text columns; pass columns=[...] explicitly")
        if key_column:
            if key_column not in available:
                raise UserError(
                    f"key_column {key_column!r} is not a column of {table}; available: {available}"
                )
            key_expr = quote_ident(key_column)
        elif kind == "view":
            raise UserError("views have no rowid; pass key_column to identify rows in the results")
        else:
            key_expr = "rowid"
        where_sql = f" WHERE ({where.strip()})" if where and where.strip() else ""
        cap = settings.search_candidates
        selected = ", ".join(quote_ident(c) for c in searched)
        sql = (
            f"SELECT {key_expr} AS __key, {selected} FROM {quote_ident(table)}"
            f"{where_sql} LIMIT {cap + 1}"
        )
        result = ac.query(sql)

    rows = result.rows
    capped = len(rows) > cap
    rows = rows[:cap]

    # One physical line per (row, column) value; newlines inside a value become a literal \n.
    index: list[tuple[object, str, bytes]] = []
    for row in rows:
        key = row[0]
        for column, value in zip(searched, row[1:], strict=True):
            if value is None or isinstance(value, bytes):
                continue
            text = str(value).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
            index.append((key, column, text.encode("utf-8", "replace")))

    hits: list[tuple[object, str, str]] = []
    if index:
        hits = _run_ripgrep(
            rg,
            index,
            pattern,
            ignore_case=ignore_case,
            fixed_strings=fixed_strings,
            context_chars=context_chars,
            timeout=settings.query_timeout,
        )

    rows_matched = len({key for key, _, _ in hits})
    rendered = render_table(
        ["key", "column", "snippet"],
        hits[:limit],
        max_bytes=settings.max_result_bytes,
        max_cell_chars=settings.max_cell_chars,
    )
    note = (
        f"# {rendered.rows} of {len(hits)} matching values in {rows_matched} rows "
        f"({len(rows)} candidate rows x {len(searched)} columns scanned)"
    )
    if capped:
        note += f"; candidate cap of {cap} rows hit, tighten the WHERE filter"
    if rendered.truncated_by_bytes:
        note += f"; output truncated at {settings.max_result_bytes} bytes"
    return SearchOutcome(
        rendered.body + note, sql, searched, len(rows), capped, len(hits), rows_matched
    )


def _run_ripgrep(
    rg: str,
    index: list[tuple[object, str, bytes]],
    pattern: str,
    *,
    ignore_case: bool,
    fixed_strings: bool,
    context_chars: int,
    timeout: float,
) -> list[tuple[object, str, str]]:
    fd, path = tempfile.mkstemp(prefix="mcp-sqlite-memory-", suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"\n".join(data for _, _, data in index) + b"\n")
        args = [rg, "--json", "--no-config", "--text"]
        if ignore_case:
            args.append("--ignore-case")
        if fixed_strings:
            args.append("--fixed-strings")
        args += ["--regexp", pattern, "--", path]
        try:
            proc = subprocess.run(
                args, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW
            )
        except subprocess.TimeoutExpired as exc:
            raise QueryTimeout(
                f"ripgrep exceeded {timeout:g}s and was cancelled; narrow the WHERE filter"
            ) from exc
    finally:
        with suppress(OSError):
            os.unlink(path)

    if proc.returncode == 1:  # ripgrep: no matches
        return []
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise UserError(f"ripgrep rejected the search: {detail}")

    hits: list[tuple[object, str, str]] = []
    for raw in proc.stdout.splitlines():
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if message.get("type") != "match":
            continue
        data = message["data"]
        line_number = data.get("line_number")
        if not line_number or line_number > len(index):
            continue
        key, column, value = index[line_number - 1]
        submatches = data.get("submatches") or []
        start = submatches[0]["start"] if submatches else 0
        end = submatches[0]["end"] if submatches else 0
        lo = max(0, start - context_chars)
        hi = min(len(value), end + context_chars)
        snippet = value[lo:hi].decode("utf-8", "replace")
        if lo > 0:
            snippet = "..." + snippet
        if hi < len(value):
            snippet += "..."
        hits.append((key, column, snippet))
    return hits
