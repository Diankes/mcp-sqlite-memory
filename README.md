# mcp-sqlite-memory

A SQLite [MCP](https://modelcontextprotocol.io) server for coding agents, built on one idea:
hand the agent SQL as its primary tool and put the safety in the server, not in the prompt.

- **SQL is the tool.** Thirteen tools, all thin wrappers around one database file.
- **Views are reusable skills.** `create_view` stores a SELECT with a description so the next
  session finds it in `get_schema` instead of rediscovering the join.
- **CSV, not JSON.** Results come back as CSV with a `# N rows` trailer, roughly half the tokens.
- **Memory the agent cannot rewrite.** An append-only, hash-chained event log plus checkpoints,
  with the hashes computed by the server.
- **Retrieved data is untrusted.** The server never builds SQL from row values, its own tables
  cannot be modified through any tool, and the connect-time instructions tell the agent that rows
  are data, not instructions.
- **Every call is audit-logged**, including the ones that failed.

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/) and, for `search_text`,
[ripgrep](https://github.com/BurntSushi/ripgrep) on `PATH`.

## Configure it in Claude Code

Setting this up specifically to back the [second-brain](https://github.com/Diankes/second-brain)
plugin? Its own README's "Install" section has the exact recipe, including the plugin's required
flags and the matching `.claude/settings.json`; the instructions below are for using this server
on its own, or as a starting point for a different consumer.

### From the terminal

From the published repository, pinned to a release tag:

```
claude mcp add sqlite-memory --scope user -- uvx --from git+https://github.com/Diankes/mcp-sqlite-memory@v0.3.0 mcp-sqlite-memory --db F:\data\memory.db
```

From a local clone while developing:

```
claude mcp add sqlite-memory --scope user -- uv --directory F:\mcp-sqlite-memory run mcp-sqlite-memory --db F:\data\memory.db
```

**Pin the tag.** `@v0.3.0` installs exactly the commit that was tested and released. A bare
repository URL tracks whatever is on `main`, so a fresh install can pick up an unreviewed
commit, and two machines set up a week apart can run different code under the same name.
Worse, `uvx` caches the environment it built, so tracking `main` does not even update
predictably: you get whichever commit was current the last time uv resolved the source, and a
different one after a cache refresh. Pin a tag, and move to a newer tag on purpose.

`--db` is the only required option; the file and its parent folder are created on first start and
the three server tables are bootstrapped automatically. Every flag can also be given as an
environment variable, for example
`claude mcp add sqlite-memory -e MCP_SQLITE_DB=F:\data\memory.db -- uvx ...`.
`uv` and `uvx` are native executables, so no `cmd /c` wrapper is needed on Windows.

### Using this with Claude Code in VS Code

The Claude Code extension has an "Add MCP server" form. For this server the Transport is always
**Local command (stdio)**, and the Arguments box takes one argument per line. There are two ways
to fill it in.

**1. From the published repository** (recommended once a version is tagged)

| Field | Value |
|---|---|
| Name | `sqlite-memory` |
| Transport | Local command (stdio) |
| Command | `uvx` |
| Arguments | see below, one per line |
| Environment variables | leave empty |
| Scope | Local |

```
--from
git+https://github.com/Diankes/mcp-sqlite-memory@v0.3.0
mcp-sqlite-memory
--db
F:\data\my-project-memory.db
```

The same tag-pinning advice applies here: `@v0.3.0` is the tested release, a bare
`git+https://github.com/Diankes/mcp-sqlite-memory` line silently tracks `main`.

Scope **Local** keeps the entry private to you and to the project you have open, which is what
makes the database per project: open another project, add the server again with a different
`--db` path, and each project gets its own memory and audit log. Choose **User** instead if you
want one shared database across every project, with one `--db` path. **Project** writes the
entry to `.mcp.json` in the repository, which is checked into git, so the `--db` path then has to
be valid on every machine that clones it.

**2. From a local clone** (while developing the server itself)

| Field | Value |
|---|---|
| Name | `sqlite-memory` |
| Transport | Local command (stdio) |
| Command | `uv` |
| Arguments | see below, one per line |
| Environment variables | leave empty |
| Scope | Local |

```
--directory
F:\mcp-sqlite-memory
run
mcp-sqlite-memory
--db
F:\data\my-project-memory.db
```

**`--db` must be an absolute path in this mode.** `--directory` tells uv to change into the
server's own folder before running, so a relative path such as `memory.db` or `data\memory.db`
resolves inside the clone (`F:\mcp-sqlite-memory\memory.db`), not inside the project you have
open in VS Code. Nothing warns you: the server creates an empty database at the wrong location,
the clone's `.gitignore` hides `*.db` files, and your project appears to have no memory at all.

Either way, the two `--db` lines can be replaced by one line `MCP_SQLITE_DB=F:\data\my-project-memory.db`
in the Environment variables box.

### Recommended permissions

Tools appear to Claude Code as `mcp__sqlite-memory__<tool>`. The one lock the server cannot
provide is the permission prompt, so allow the twelve ordinary tools and leave
`destructive_query` on ask. Paste this into `.claude/settings.json` in the project, or into
`~/.claude/settings.json` for every project:

```json
{
  "permissions": {
    "allow": [
      "mcp__sqlite-memory__read_query",
      "mcp__sqlite-memory__write_query",
      "mcp__sqlite-memory__list_tables",
      "mcp__sqlite-memory__describe_table",
      "mcp__sqlite-memory__get_schema",
      "mcp__sqlite-memory__create_view",
      "mcp__sqlite-memory__append_event",
      "mcp__sqlite-memory__snapshot",
      "mcp__sqlite-memory__verify_chain",
      "mcp__sqlite-memory__checkpoint",
      "mcp__sqlite-memory__get_resume_context",
      "mcp__sqlite-memory__search_text"
    ],
    "ask": ["mcp__sqlite-memory__destructive_query"]
  }
}
```

### If another tool needs to read this database directly

This server has no way to publish the `--db` path it was registered with — nothing else on the
machine can discover it automatically. A plugin such as
[second-brain](https://github.com/Diankes/second-brain), which opens its own direct, read-only
connection to the same file (for validation, cadence checks and rules injection, without going
through the server) to work at all, needs to be told that same path a second time, independently
— typically as its own `MCP_SQLITE_DB` environment variable in the consuming project's
`.claude/settings.json`. Nothing here checks that the two agree: if you move the database or
change one registration and not the other, each side keeps working against its own path,
silently. See second-brain's README, "Where things live, and the one path that must match," for
the full picture and a worked example.

## Tools

| Tool | What it does | Returns |
|---|---|---|
| `read_query(query, limit=100)` | One SELECT, WITH ... SELECT, EXPLAIN or introspection PRAGMA. Read-only connection, wall-clock timeout, row and byte caps. | CSV plus a `# N rows` trailer that says whether the result was cut |
| `write_query(query)` | One INSERT, UPDATE, upsert, REPLACE, CREATE TABLE, CREATE INDEX or CREATE VIRTUAL TABLE (fts5, rtree). An UPDATE touching more than `--max-update-rows` rows is rolled back. | `ok: 3 rows affected (last_insert_rowid=17)`; RETURNING rows as CSV |
| `destructive_query(query, reason)` | One DELETE, DROP, ALTER TABLE, CREATE/DROP TRIGGER or unfiltered UPDATE. Snapshots the database file first. | `ok: 120 rows affected; snapshot: <path>` |
| `list_tables(include_row_counts=true)` | Tables and views, with a `system` flag for the server's own tables. | CSV: `name,type,system,rows` |
| `describe_table(table)` | Columns, foreign keys and indexes of a table or view. | CSV sections |
| `get_schema(include_system=false)` | Every CREATE statement as stored, view descriptions included. | SQL text |
| `create_view(name, select_sql, description="", replace=false)` | Save a SELECT as a named view with a description stored inside it. | `ok: view recent created` |
| `append_event(content, kind="note")` | Append to the hash-chained memory log. | `ok: event 42 appended (hash 3f9a1c...)` |
| `snapshot(reason="manual")` | On-demand snapshot of the database file, same online backup API as `destructive_query`, same rotation. Respects `--snapshots` (0 disables) and `--snapshot-dir`; an unreachable target is reported, never raised. | `ok: snapshot written: <path>` or `ok: snapshot skipped (disabled or target unavailable)` |
| `verify_chain()` | Recompute the whole chain. | `{"ok": true, "events": 42, ...}` or the first breaking event id and why |
| `checkpoint(summary)` | Store a state summary anchored to the current chain head. | `ok: checkpoint 7 written at event 42` |
| `get_resume_context(max_events=50, exclude_kinds="")` | Latest checkpoint, events since it, chain status. Call it first in a new session. `exclude_kinds` is a comma-separated list of kinds to hide, for example hook-written raw events. When the events overflow the byte cap the oldest are dropped, never the newest. | Text block plus CSV |
| `search_text(pattern, table, columns=null, where=null, key_column=null, limit=100, ignore_case=false, fixed_strings=false, context_chars=80)` | SQL narrows the rows, ripgrep matches the pattern over the selected columns. Newlines inside a value appear as `⏎` in the search and the snippet, so LaTeX commands such as `\nabla` are never confused with line breaks. | CSV: `key,column,snippet` plus totals |

Errors come back verbatim as tool errors (`no such table: nope`, `DROP TABLE is not allowed in
write_query; use destructive_query`, ...) so the agent can fix the statement and retry.

## Memory layer

Three tables are created on first start:

- `memory_events(id, ts, session, kind, content, prev_hash, hash)`: append-only. The server
  computes `hash = sha256([id, ts, session, kind, content, prev_hash])` and links every row to the
  one before it; the first row links to 64 zeros. `session` is a per-server-run id, stamped
  automatically.
- `memory_checkpoints(id, ts, session, summary, last_event_id, head_hash)`: append-only.
  A checkpoint points at the chain head it was written against.
- `query_log(id, ts, session, tool, query, ok, rows, error, duration_ms, detail)`: every tool
  call, success or failure. The built-in view `query_errors` lists the failures newest first.

`get_resume_context` verifies the chain on every call, so tampering is noticed at the moment it
matters. Be clear about what that buys on a single-user machine: the chain is tamper-evident, not
tamper-proof. Anyone with the sqlite3 CLI can rewrite it and recompute the hashes. What actually
stops the tools from editing history is the authorizer policy plus BEFORE UPDATE / BEFORE DELETE
triggers on all three tables.

## Safety rails

All of these live in the server; none depend on the agent behaving.

- **Parse-time statement policy.** SQLite's authorizer callback classifies every action a
  statement performs while it is compiled, so comments, CTEs and casing cannot disguise a DROP.
  Read tools allow SELECT, READ and introspection PRAGMAs only; `write_query` adds INSERT, UPDATE
  and additive DDL; `destructive_query` adds DELETE, DROP, ALTER and triggers. ATTACH, transaction
  control, PRAGMA writes, temporary objects and `load_extension` are refused everywhere.
- **Protected tables.** No tool can insert into, update, delete from, drop, alter or attach a
  trigger to the three server tables, and triggers enforce append-only even for direct
  connections.
- **Read-only connections** for the read tools (`PRAGMA query_only`) and SQLite's defensive mode
  on every agent connection, as backstops behind the authorizer.
- **Wall-clock timeout** per call through SQLite's progress handler; a runaway query is
  interrupted and reported. The ripgrep subprocess gets the same budget.
- **Three independent result caps:** rows (fetched as `limit + 1`, never by rewriting your SQL),
  total bytes, and characters per cell.
- **UPDATE rowcount guard** in `write_query`: a forgotten WHERE clause is rolled back instead of
  rewriting a table.
- **Snapshots** before every `destructive_query`, and on demand through the `snapshot` tool,
  made with SQLite's online backup API and kept in `<db>.snapshots/` next to the database
  (or under `--snapshot-dir`, for example a removable drive), newest five by default. A
  statement that fails syntax or policy checks costs no snapshot, and a snapshot target that
  cannot be reached is skipped with a warning rather than failing the call.
- **One statement per call**, enforced by Python's sqlite3 module.
- **Parameterized SQL** wherever the server builds statements; identifiers are validated and
  quoted.
- **Audit log written in a `finally` block** on its own connection, so an errored call still
  gets a row.

## Configuration

| Flag | Environment variable | Default |
|---|---|---|
| `--db PATH` | `MCP_SQLITE_DB` | required |
| `--query-timeout SECONDS` | `MCP_SQLITE_QUERY_TIMEOUT` | 10 |
| `--default-limit N` | `MCP_SQLITE_DEFAULT_LIMIT` | 100 |
| `--max-rows N` | `MCP_SQLITE_MAX_ROWS` | 1000 |
| `--max-result-bytes N` | `MCP_SQLITE_MAX_RESULT_BYTES` | 32768 |
| `--max-cell-chars N` | `MCP_SQLITE_MAX_CELL_CHARS` | 2000 |
| `--max-update-rows N` | `MCP_SQLITE_MAX_UPDATE_ROWS` | 500 |
| `--search-candidates N` | `MCP_SQLITE_SEARCH_CANDIDATES` | 5000 |
| `--snapshots N` | `MCP_SQLITE_SNAPSHOTS` | 5 (0 disables) |
| `--snapshot-dir PATH` | `MCP_SQLITE_SNAPSHOT_DIR` | `<db>.snapshots/` next to the database |
| `--rg PATH` | `MCP_SQLITE_RG` | `rg` on PATH |
| `-v`, `--verbose` | `MCP_SQLITE_VERBOSE` | off |

**Long-form notes.** The defaults suit tabular data. If the database holds prose with display
math, such as a study log written through the memory tools, raise the two read caps so entries
come back whole: `--max-cell-chars 8000 --max-result-bytes 131072`. Multi-line cells are valid
CSV and are returned as written; nothing is escaped on the read path.

## Restoring a snapshot

Snapshots are plain SQLite files. Stop the server (quit the Claude Code session that started it),
copy the snapshot over the database file, start again. There is deliberately no restore tool:
swapping the file under an open connection is not safe.

## Closing the loop

The audit log is where the schema gets better. `SELECT * FROM query_errors LIMIT 20` shows which
statements keep failing; a repeated join becomes a `create_view`; a column the agent keeps
misreading gets a clearer name. The server's own `query_errors` view is the first example of the
pattern.

## Development

```
uv sync --all-groups
uv run pytest
uv run ruff check src tests
uv run ruff format src tests
```

Tests run the server in-process through `mcp.client.Client` for the tool surface, and drive the
policy, chain, snapshot and search code directly. The search tests skip when ripgrep is absent.

## License

MIT, see `LICENSE`.
