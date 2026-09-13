"""Runtime configuration."""

from dataclasses import dataclass
from pathlib import Path

ENV_PREFIX = "MCP_SQLITE_"


@dataclass(frozen=True)
class Settings:
    """Every field maps to a CLI flag and to an ``MCP_SQLITE_*`` environment variable."""

    db_path: Path
    query_timeout: float = 10.0  # wall-clock seconds per tool call
    default_limit: int = 100  # rows read_query returns when the agent gives no limit
    max_rows: int = 1000  # hard ceiling on rows per result, whatever the agent asks for
    max_result_bytes: int = 32_768  # hard ceiling on CSV bytes per result (about 8k tokens)
    max_cell_chars: int = 2_000  # a single cell is cut beyond this many characters
    max_update_rows: int = 500  # write_query rolls back an UPDATE that touches more rows
    search_candidates: int = 5_000  # rows the SQL filter may hand to ripgrep in search_text
    snapshots: int = 5  # snapshot files kept per database; 0 disables snapshots
    snapshot_dir: Path | None = None  # snapshot target; None = <db>.snapshots/ by the db
    rg_path: str | None = None  # explicit ripgrep executable; None looks up "rg" on PATH
    verbose: bool = False

    def __post_init__(self) -> None:
        positive = {
            "query_timeout": self.query_timeout,
            "default_limit": self.default_limit,
            "max_rows": self.max_rows,
            "max_result_bytes": self.max_result_bytes,
            "max_cell_chars": self.max_cell_chars,
            "max_update_rows": self.max_update_rows,
            "search_candidates": self.search_candidates,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
        if self.snapshots < 0:
            raise ValueError("snapshots cannot be negative")
        if self.default_limit > self.max_rows:
            raise ValueError("default_limit cannot exceed max_rows")
