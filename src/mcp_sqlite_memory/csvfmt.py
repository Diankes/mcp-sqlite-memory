"""CSV rendering with row, byte and cell caps."""

import csv
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass


@dataclass
class Rendered:
    body: str  # header line plus data lines, each newline-terminated
    rows: int
    truncated_by_bytes: bool
    cells_cut: int


def format_cell(value: object, max_chars: int) -> tuple[str, bool]:
    """Text for one cell, and whether it had to be cut."""
    if value is None:
        return "", False
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>", False
    text = repr(value) if isinstance(value, float) else str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}...[+{len(text) - max_chars} chars]", True
    return text, False


def _csv_line(cells: Sequence[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(cells)
    return buf.getvalue()


def render_table(
    columns: Sequence[str],
    rows: Iterable[Sequence[object]],
    *,
    max_bytes: int,
    max_cell_chars: int,
) -> Rendered:
    """Header plus as many rows as fit in max_bytes (always at least one when rows exist)."""
    header = _csv_line(list(columns))
    parts = [header]
    size = len(header.encode("utf-8"))
    written = 0
    cells_cut = 0
    truncated = False
    for row in rows:
        cells = []
        for value in row:
            text, cut = format_cell(value, max_cell_chars)
            cells.append(text)
            cells_cut += int(cut)
        line = _csv_line(cells)
        line_size = len(line.encode("utf-8"))
        if written and size + line_size > max_bytes:
            truncated = True
            break
        parts.append(line)
        size += line_size
        written += 1
    return Rendered("".join(parts), written, truncated, cells_cut)


def trailer(
    rendered: Rendered,
    *,
    more_rows: bool,
    limit: int | None,
    max_limit: int,
    max_bytes: int,
    max_cell_chars: int,
) -> str:
    """The '# N rows' line that closes every result and says whether it was cut."""
    note = f"# {rendered.rows} rows"
    if rendered.truncated_by_bytes:
        note += f", truncated at {max_bytes} bytes (select fewer columns or narrow the query)"
    elif more_rows:
        note += (
            f", truncated by limit={limit} (more rows exist; raise limit up to {max_limit} "
            "or narrow the query)"
        )
    if rendered.cells_cut:
        note += f"; {rendered.cells_cut} cells cut to {max_cell_chars} chars"
    return note
