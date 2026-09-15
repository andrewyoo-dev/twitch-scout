"""Tests for the CLI wiring.

Network-touching flow (a real ``collect``) is covered by the collector's own tests;
here we verify argument parsing, the init-db integration, and that a collect with no
credentials fails cleanly with exit code 1 before any API call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twitch_scout.cli import build_parser, main


def test_parser_collect_defaults() -> None:
    args = build_parser().parse_args(["collect"])
    assert args.command == "collect"
    assert args.tier == "auto"
    assert args.top is None
    assert args.force is False


def test_parser_collect_overrides() -> None:
    args = build_parser().parse_args(["collect", "--tier", "window", "--top", "10", "--force"])
    assert args.tier == "window"
    assert args.top == 10
    assert args.force is True


def test_parser_rejects_unknown_tier() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["collect", "--tier", "nonsense"])


def test_parser_requires_a_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_init_db_creates_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "scout.db"
    monkeypatch.setenv("SCOUT_DB", str(db))
    monkeypatch.delenv("SCOUT_TOP_N", raising=False)

    code = main(["init-db"])

    assert code == 0
    assert db.exists()
    assert "schema v1" in capsys.readouterr().out


def test_collect_without_credentials_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)

    code = main(["collect"])

    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_bad_env_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_TOP_N", "banana")
    code = main(["init-db"])
    assert code == 1
    assert "integer" in capsys.readouterr().err
