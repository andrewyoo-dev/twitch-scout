"""Tests for the CLI wiring.

Network-touching flow (a real ``collect``) is covered by the collector's own tests;
here we verify argument parsing, the init-db integration, and that a collect with no
credentials fails cleanly with exit code 1 before any API call.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from twitch_scout.cli import build_parser, main
from twitch_scout.twitch.models import HelixGame


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


def test_max_channels_also_caps_the_owned_section() -> None:
    # --max-channels must reach owned_guards, or giant owned categories dominate the
    # owned section (the opposite of surfacing low-competition owned games).
    from twitch_scout.cli import _rank_config_from_args

    config = _rank_config_from_args(build_parser().parse_args(["rank", "--max-channels", "50"]))
    assert config.guards.max_avg_channels == 50.0
    assert config.owned_guards is not None
    assert config.owned_guards.max_avg_channels == 50.0
    # The owned floor stays relaxed (not tied to the main floor).
    assert config.owned_guards.min_viewer_floor < config.guards.min_viewer_floor


@pytest.mark.parametrize(
    ("ceiling", "owned_disabled"),
    [("0", True), ("0.5", True), ("1", False), ("50", False)],
)
def test_sub_min_ceiling_disables_owned_without_loosening_min(
    ceiling: str, owned_disabled: bool
) -> None:
    # A ceiling below the owned minimum (1) disables the owned section rather than
    # loosening its minimum to force construction (Astra R3).
    from twitch_scout.cli import _rank_config_from_args
    from twitch_scout.rank.rank import relaxed_owned_guards

    args = build_parser().parse_args(["rank", "--min-channels", "0", "--max-channels", ceiling])
    config = _rank_config_from_args(args)
    if owned_disabled:
        assert config.owned_guards is None
    else:
        assert config.owned_guards is not None
        # The owned minimum is never loosened below its relaxed default.
        assert config.owned_guards.min_avg_channels == relaxed_owned_guards().min_avg_channels


def test_collect_top_overrides_both_tiers(monkeypatch: pytest.MonkeyPatch) -> None:
    from twitch_scout import cli

    seen = {}

    class StopHere(Exception):
        pass

    def fake_collector(*args: object, config: object, **kwargs: object) -> object:
        seen["config"] = config
        raise StopHere

    monkeypatch.setenv("TWITCH_CLIENT_ID", "x")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "x")
    monkeypatch.setenv("SCOUT_DB", ":memory:")
    monkeypatch.setattr(cli, "Collector", fake_collector)
    with pytest.raises(StopHere):
        main(["collect", "--top", "20"])
    config = seen["config"]
    assert (config.top_n, config.window_top_n) == (20, 20)  # type: ignore[attr-defined]


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
    assert "schema v3" in capsys.readouterr().out


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


def test_steam_sync_without_credentials_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    for key in ("STEAM_API_KEY", "STEAM_ID"):
        monkeypatch.delenv(key, raising=False)

    code = main(["steam-sync"])

    assert code == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "STEAM_API_KEY" in err


def test_httpx_logging_is_muted_to_protect_the_steam_key() -> None:
    # httpx logs request URLs at INFO; the Steam API key rides in the query string,
    # so httpx must stay at WARNING (even under --verbose) or the key leaks to logs.
    import logging

    from twitch_scout.cli import _configure_logging

    _configure_logging(verbose=True)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_steam_sync_malformed_alias_file_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from twitch_scout import cli

    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    for key in ("STEAM_API_KEY", "STEAM_ID", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        monkeypatch.setenv(key, "x")

    def broken() -> dict[str, str]:
        raise ValueError("aliases.toml: invalid entry 'X' = ''")

    monkeypatch.setattr(cli, "load_aliases", broken)

    code = main(["steam-sync"])

    assert code == 1
    assert "aliases.toml" in capsys.readouterr().err


class _FakeHelixCM:
    """Stands in for HelixClient.create(...) in `watch add`: context manager + lookup."""

    def __init__(self, categories: dict[str, str], search: dict[str, list[str]]) -> None:
        self._categories = categories
        self._search = search

    def __enter__(self) -> _FakeHelixCM:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        wanted = {n.casefold() for n in names}
        return [
            HelixGame(id=i, name=n) for n, i in self._categories.items() if n.casefold() in wanted
        ]

    def search_categories(self, query: str) -> list[HelixGame]:
        return [HelixGame(id=f"s-{n}", name=n) for n in self._search.get(query, [])]


def _watch_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helix: _FakeHelixCM) -> None:
    from twitch_scout import cli

    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    monkeypatch.setenv("TWITCH_CLIENT_ID", "x")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "x")
    monkeypatch.setattr(cli.HelixClient, "create", lambda *a, **k: helix)


def test_parser_watch_subcommands() -> None:
    parser = build_parser()
    assert parser.parse_args(["watch", "add", "A", "B C"]).names == ["A", "B C"]
    assert parser.parse_args(["watch", "remove", "A"]).names == ["A"]
    assert parser.parse_args(["watch", "list"]).watch_command == "list"
    with pytest.raises(SystemExit):
        parser.parse_args(["watch"])


def test_watch_add_list_remove_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helix = _FakeHelixCM({"Anime Shop Simulator": "10"}, {})
    _watch_env(tmp_path, monkeypatch, helix)

    assert main(["watch", "add", "anime shop simulator"]) == 0
    assert "watching: Anime Shop Simulator" in capsys.readouterr().out

    assert main(["watch", "list"]) == 0
    assert "Anime Shop Simulator" in capsys.readouterr().out

    assert main(["watch", "remove", "Anime Shop Simulator"]) == 0
    assert "removed: Anime Shop Simulator" in capsys.readouterr().out

    assert main(["watch", "list"]) == 0
    assert "watchlist is empty" in capsys.readouterr().out


def test_watch_add_unresolved_shows_suggestions_and_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    helix = _FakeHelixCM({}, {"Anime Shop": ["Anime Shop Simulator", "Anime Shop Tycoon"]})
    _watch_env(tmp_path, monkeypatch, helix)

    assert main(["watch", "add", "Anime Shop"]) == 1
    out = capsys.readouterr().out
    assert 'no verified Twitch category for "Anime Shop"' in out
    assert "Anime Shop Simulator | Anime Shop Tycoon" in out


def test_watch_remove_unknown_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    assert main(["watch", "remove", "Nope"]) == 1
    assert 'not on the watchlist: "Nope"' in capsys.readouterr().out


def test_watch_add_without_credentials_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_DB", str(tmp_path / "scout.db"))
    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)
    assert main(["watch", "add", "Anything"]) == 1
    assert "TWITCH_CLIENT_ID" in capsys.readouterr().err


def test_bad_env_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCOUT_TOP_N", "banana")
    code = main(["init-db"])
    assert code == 1
    assert "integer" in capsys.readouterr().err
