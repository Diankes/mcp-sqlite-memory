from mcp_sqlite_memory.csvfmt import format_cell, render_table, trailer


def test_cells_quote_null_blob_float_and_cut():
    assert format_cell(None, 10) == ("", False)
    assert format_cell(b"\x00\x01", 10) == ("<blob 2 bytes>", False)
    assert format_cell(2.0, 10) == ("2.0", False)
    assert format_cell("x" * 12, 10) == ("x" * 10 + "...[+2 chars]", True)


def test_render_quotes_commas_quotes_and_newlines():
    rendered = render_table(
        ["a", "b"], [('he said "hi", twice', "two\nlines")], max_bytes=1000, max_cell_chars=100
    )
    assert rendered.body == 'a,b\n"he said ""hi"", twice","two\nlines"\n'
    assert rendered.rows == 1 and not rendered.truncated_by_bytes and rendered.cells_cut == 0


def test_render_stops_at_the_byte_cap_but_keeps_one_row():
    rows = [("x" * 50,) for _ in range(10)]
    rendered = render_table(["a"], rows, max_bytes=120, max_cell_chars=100)
    assert rendered.rows == 2 and rendered.truncated_by_bytes
    tiny = render_table(["a"], rows, max_bytes=5, max_cell_chars=100)
    assert tiny.rows == 1 and tiny.truncated_by_bytes


def test_trailer_wording():
    rendered = render_table(["a"], [(1,), (2,)], max_bytes=1000, max_cell_chars=100)
    common = {"max_limit": 1000, "max_bytes": 1000, "max_cell_chars": 100}
    assert trailer(rendered, more_rows=False, limit=2, **common) == "# 2 rows"
    assert trailer(rendered, more_rows=True, limit=2, **common) == (
        "# 2 rows, truncated by limit=2 "
        "(more rows exist; raise limit up to 1000 or narrow the query)"
    )
    cut = render_table(["a"], [("y" * 200,)], max_bytes=1000, max_cell_chars=100)
    assert trailer(cut, more_rows=False, limit=1, **common) == "# 1 rows; 1 cells cut to 100 chars"
    capped = render_table(["a"], [("z" * 50,)] * 5, max_bytes=80, max_cell_chars=100)
    assert trailer(capped, more_rows=True, limit=5, **common).startswith(
        "# 1 rows, truncated at 1000 bytes"
    )
