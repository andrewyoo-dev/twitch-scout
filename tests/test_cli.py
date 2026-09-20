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


def test_parser_rank_defaults() -> None:
    args = build_parser().parse_args(["rank"])
    assert args.command == "rank"
    assert args.days == 14
    assert args.floor is None
    assert args.min_channels is None
    assert args.max_channels is None
    assert args.concentration_penalty is None
    assert args.limit == 25
    assert args.hide_falling is False
    assert args.show_rejected is False


def test_parser_rank_max_channels() -> None:
    args = build_parser().parse_args(["rank", "--max-channels", "50"])
    assert args.max_channels == 50.0


def test_parser_rank_concentration_penalty() -> None:
    args = build_parser().parse_args(["rank", "--concentration-penalty", "0.7"])
    assert args.concentration_penalty == 0.7


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


def test_rank_bad_concentration_penalty_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "scout.db"
    monkeypatch.setenv("SCOUT_DB", str(db))
    main(["init-db"])
    capsys.readouterr()

    code = main(["rank", "--concentration-penalty", "2"])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "concentration_penalty" in err


def test_rank_max_channels_below_min_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "scout.db"
    monkeypatch.setenv("SCOUT_DB", str(db))
    main(["init-db"])
    capsys.readouterr()

    code = main(["rank", "--max-channels", "2"])

    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_collect_bad_top_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "x")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "x")

    code = main(["collect", "--top", "-5"])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "top_n" in err


@pytest.mark.parametrize("limit", ["0", "-3"])
def test_rank_bad_limit_exits_cleanly(
    limit: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    main(["init-db"])
    capsys.readouterr()

    code = main(["rank", "--limit", limit])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "limit" in err


@pytest.mark.parametrize("flag", ["--floor", "--min-channels", "--max-channels"])
def test_rank_nan_float_exits_cleanly(
    flag: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    main(["init-db"])
    capsys.readouterr()

    code = main(["rank", flag, "nan"])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "finite" in err


def test_rank_excessive_days_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", ":memory:")

    code = main(["rank", "--days", "999999999"])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "eval_days" in err


def test_bad_env_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_TOP_N", "banana")
    code = main(["init-db"])
    assert code == 1
    assert "integer" in capsys.readouterr().err
