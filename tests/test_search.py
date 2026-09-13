import pytest

from mcp_sqlite_memory.errors import UserError
from mcp_sqlite_memory.search import NEWLINE_MARK, find_ripgrep, search_text

RG = find_ripgrep(None)
pytestmark = pytest.mark.skipif(RG is None, reason="ripgrep is not installed")


def search(db, settings, **overrides):
    args = {
        "pattern": "apple",
        "table": "notes",
        "columns": None,
        "where": None,
        "key_column": None,
        "limit": 10,
        "ignore_case": False,
        "fixed_strings": False,
        "context_chars": 10,
    }
    args.update(overrides)
    return search_text(db, settings, RG, **args)


def test_basic_match_reports_key_column_and_snippet(seeded, settings):
    out = search(seeded, settings)
    assert out.matches == 1 and out.rows_matched == 1 and out.columns == ["title", "body"]
    assert out.text == (
        "key,column,snippet\n1,body,...ote about apples\n"
        "# 1 of 1 matching values in 1 rows (3 candidate rows x 2 columns scanned)"
    )


def test_where_narrows_before_ripgrep(seeded, settings):
    out = search(seeded, settings, pattern="note", where="id > 1")
    assert out.matches == 1 and "\n2,body," in out.text
    assert "WHERE (id > 1)" in out.sql


def test_case_and_fixed_strings(seeded, settings):
    assert search(seeded, settings, pattern="APPLES").matches == 0
    assert search(seeded, settings, pattern="APPLES", ignore_case=True).matches == 1
    assert search(seeded, settings, pattern='"quotes"', fixed_strings=True).matches == 1
    assert search(seeded, settings, pattern="note.", fixed_strings=True).matches == 0
    assert search(seeded, settings, pattern="note.").matches == 2


def test_invalid_regex_is_reported(seeded, settings):
    with pytest.raises(UserError, match="ripgrep rejected"):
        search(seeded, settings, pattern="(")


def test_column_and_table_validation(seeded, settings):
    with pytest.raises(UserError, match="unknown column"):
        search(seeded, settings, columns=["nope"])
    with pytest.raises(UserError, match="no table or view"):
        search(seeded, settings, table="nope")
    with pytest.raises(UserError, match="key_column"):
        search(seeded, settings, key_column="nope")
    with pytest.raises(UserError, match="pattern must not be empty"):
        search(seeded, settings, pattern="")


def test_views_need_an_explicit_key(seeded, settings):
    with seeded.internal() as conn:
        conn.execute("CREATE VIEW v AS SELECT id AS note_id, body FROM notes")
    with pytest.raises(UserError, match="key_column"):
        search(seeded, settings, table="v")
    out = search(seeded, settings, table="v", key_column="note_id", columns=["body"])
    assert out.matches == 1 and out.text.startswith("key,column,snippet\n1,body,")


def test_newlines_inside_values_stay_on_one_line(seeded, settings):
    with seeded.internal() as conn:
        conn.execute("INSERT INTO notes(title, body) VALUES ('multi', 'line one\nline two')")
        conn.execute("INSERT INTO notes(title, body) VALUES ('latex', 'grad is $\\nabla f$')")
    out = search(seeded, settings, pattern="one" + NEWLINE_MARK + "line", context_chars=0)
    assert out.matches == 1 and "one" + NEWLINE_MARK + "line" in out.text
    # A LaTeX \n... command is not confused with an escaped newline in either direction.
    assert search(seeded, settings, pattern="\\\\nabla", fixed_strings=False).matches == 1
    assert search(seeded, settings, pattern="one\\\\nline").matches == 0


def test_candidate_cap_is_reported(seeded, settings):
    with seeded.internal() as conn:
        conn.executemany(
            "INSERT INTO notes(title, body) VALUES (?, ?)",
            [(f"t{i}", f"filler {i}") for i in range(150)],
        )
    out = search(seeded, settings, pattern="filler", limit=5)
    assert out.capped and out.candidates == settings.search_candidates
    assert "candidate cap of 100 rows hit" in out.text
    assert out.text.count("\n") == 6  # header + 5 rows + trailer


def test_missing_ripgrep_is_a_clear_error(seeded, settings):
    with pytest.raises(UserError, match="ripgrep"):
        search_text(
            seeded,
            settings,
            None,
            pattern="x",
            table="notes",
            columns=None,
            where=None,
            key_column=None,
            limit=10,
            ignore_case=False,
            fixed_strings=False,
            context_chars=10,
        )
