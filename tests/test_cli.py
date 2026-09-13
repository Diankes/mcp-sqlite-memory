from pathlib import Path

from mcp_sqlite_memory.cli import build_parser, settings_from_args


def test_snapshot_dir_flag_and_default(tmp_path):
    args = build_parser().parse_args(
        ["--db", str(tmp_path / "x.db"), "--snapshot-dir", "E:/backups"]
    )
    assert settings_from_args(args).snapshot_dir == Path("E:/backups")
    plain = build_parser().parse_args(["--db", str(tmp_path / "x.db")])
    assert settings_from_args(plain).snapshot_dir is None


def test_snapshot_dir_env_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_SQLITE_SNAPSHOT_DIR", str(tmp_path / "usb"))
    args = build_parser().parse_args(["--db", str(tmp_path / "x.db")])
    assert settings_from_args(args).snapshot_dir == tmp_path / "usb"
