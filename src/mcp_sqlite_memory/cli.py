"""Command-line entry point: parse flags and environment, then serve over stdio."""

import argparse
import logging
import os
import sys
from pathlib import Path

from mcp_sqlite_memory import __version__
from mcp_sqlite_memory.settings import ENV_PREFIX, Settings


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(ENV_PREFIX + name, default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-sqlite-memory",
        description=(
            "SQLite MCP server with a tamper-evident memory layer, an automatic audit log "
            "and server-side safety rails. Every flag can also be set through the "
            "environment variable shown in brackets."
        ),
    )
    parser.add_argument(
        "--db",
        default=_env("DB"),
        metavar="PATH",
        help="SQLite database file; created if missing [MCP_SQLITE_DB]",
    )
    parser.add_argument(
        "--query-timeout",
        type=float,
        default=float(_env("QUERY_TIMEOUT", "10")),
        metavar="SECONDS",
        help="wall-clock budget per tool call (default 10) [MCP_SQLITE_QUERY_TIMEOUT]",
    )
    parser.add_argument(
        "--default-limit",
        type=int,
        default=int(_env("DEFAULT_LIMIT", "100")),
        metavar="N",
        help="rows read_query returns when no limit is given (default 100) "
        "[MCP_SQLITE_DEFAULT_LIMIT]",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=int(_env("MAX_ROWS", "1000")),
        metavar="N",
        help="hard ceiling on rows per result (default 1000) [MCP_SQLITE_MAX_ROWS]",
    )
    parser.add_argument(
        "--max-result-bytes",
        type=int,
        default=int(_env("MAX_RESULT_BYTES", "32768")),
        metavar="N",
        help="hard ceiling on CSV bytes per result (default 32768) [MCP_SQLITE_MAX_RESULT_BYTES]",
    )
    parser.add_argument(
        "--max-cell-chars",
        type=int,
        default=int(_env("MAX_CELL_CHARS", "2000")),
        metavar="N",
        help="cut a single cell beyond this many characters (default 2000) "
        "[MCP_SQLITE_MAX_CELL_CHARS]",
    )
    parser.add_argument(
        "--max-update-rows",
        type=int,
        default=int(_env("MAX_UPDATE_ROWS", "500")),
        metavar="N",
        help="write_query rolls back an UPDATE touching more rows (default 500) "
        "[MCP_SQLITE_MAX_UPDATE_ROWS]",
    )
    parser.add_argument(
        "--search-candidates",
        type=int,
        default=int(_env("SEARCH_CANDIDATES", "5000")),
        metavar="N",
        help="rows the SQL filter may hand to ripgrep in search_text (default 5000) "
        "[MCP_SQLITE_SEARCH_CANDIDATES]",
    )
    parser.add_argument(
        "--snapshots",
        type=int,
        default=int(_env("SNAPSHOTS", "5")),
        metavar="N",
        help="snapshots kept before destructive statements; 0 disables (default 5) "
        "[MCP_SQLITE_SNAPSHOTS]",
    )
    parser.add_argument(
        "--rg",
        default=_env("RG"),
        metavar="PATH",
        help="ripgrep executable (default: rg on PATH) [MCP_SQLITE_RG]",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=(_env("VERBOSE", "") or "").lower() in ("1", "true", "yes"),
        help="debug logging on stderr [MCP_SQLITE_VERBOSE]",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        db_path=Path(args.db),
        query_timeout=args.query_timeout,
        default_limit=args.default_limit,
        max_rows=args.max_rows,
        max_result_bytes=args.max_result_bytes,
        max_cell_chars=args.max_cell_chars,
        max_update_rows=args.max_update_rows,
        search_candidates=args.search_candidates,
        snapshots=args.snapshots,
        rg_path=args.rg,
        verbose=args.verbose,
    )


def configure_logging(verbose: bool) -> None:
    # stdout carries the MCP protocol; every diagnostic goes to stderr, as UTF-8 even on
    # Windows consoles whose default code page cannot encode what a query may contain.
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.db:
        parser.error("--db PATH is required (or set MCP_SQLITE_DB)")
    configure_logging(args.verbose)
    try:
        settings = settings_from_args(args)
    except ValueError as exc:
        parser.error(str(exc))

    from mcp_sqlite_memory.server import build_server  # deferred: keeps --help and --version quick

    server = build_server(settings)
    server.run(transport="stdio")
