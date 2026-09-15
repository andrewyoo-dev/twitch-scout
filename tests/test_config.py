"""Tests for environment configuration."""

from __future__ import annotations

import pytest

from twitch_scout.config import (
    DEFAULT_DB,
    DEFAULT_STREAMS_MAX_PAGES,
    DEFAULT_TOP_N,
    Config,
    ConfigError,
)


def test_defaults_when_env_empty() -> None:
    config = Config.from_env({})
    assert config.db == DEFAULT_DB
    assert config.collector.top_n == DEFAULT_TOP_N
    assert config.collector.streams_max_pages == DEFAULT_STREAMS_MAX_PAGES
    assert config.client_id is None


def test_reads_overrides() -> None:
    config = Config.from_env(
        {
            "SCOUT_DB": "/data/scout.db",
            "SCOUT_TOP_N": "300",
            "SCOUT_STREAMS_MAX_PAGES": "1",
            "TWITCH_CLIENT_ID": "cid",
            "TWITCH_CLIENT_SECRET": "secret",
        }
    )
    assert config.db == "/data/scout.db"
    assert config.collector.top_n == 300
    assert config.collector.streams_max_pages == 1


def test_require_twitch_returns_credentials() -> None:
    config = Config.from_env({"TWITCH_CLIENT_ID": "cid", "TWITCH_CLIENT_SECRET": "s"})
    creds = config.require_twitch()
    assert creds.client_id == "cid"
    assert creds.client_secret == "s"


def test_require_twitch_raises_when_missing() -> None:
    config = Config.from_env({"TWITCH_CLIENT_ID": "cid"})  # secret missing
    with pytest.raises(ConfigError, match="must be set"):
        config.require_twitch()


def test_non_integer_env_is_rejected() -> None:
    with pytest.raises(ConfigError, match="SCOUT_TOP_N must be an integer"):
        Config.from_env({"SCOUT_TOP_N": "lots"})


def test_out_of_range_env_is_rejected() -> None:
    # CollectorConfig validation surfaces as a ConfigError.
    with pytest.raises(ConfigError, match="top_n"):
        Config.from_env({"SCOUT_TOP_N": "0"})
