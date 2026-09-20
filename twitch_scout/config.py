"""Configuration loaded from the environment.

The environment is a trust boundary, so values are validated as they are read
(coding standard 3): a malformed number fails with a clear message rather than
surfacing later as a mysterious error. The mapping is injectable so tests do not
have to mutate the real process environment (standard 9).

Twitch credentials are optional at load time — ``init-db`` needs none — and required
lazily by the commands that actually call the API.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from twitch_scout.collect.collector import CollectorConfig

DEFAULT_DB = "scout.db"
DEFAULT_TOP_N = 500
DEFAULT_STREAMS_MAX_PAGES = 3


class ConfigError(RuntimeError):
    """A configuration value is missing or malformed."""


@dataclass(frozen=True)
class TwitchCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class SteamCredentials:
    api_key: str
    steam_id: str  # a steamid64 or a vanity name; the client resolves either


@dataclass(frozen=True)
class Config:
    db: str
    collector: CollectorConfig
    client_id: str | None
    client_secret: str | None
    turso_auth_token: str | None
    steam_api_key: str | None
    steam_id: str | None

    def require_twitch(self) -> TwitchCredentials:
        if not self.client_id or not self.client_secret:
            raise ConfigError(
                "TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET must be set for this command"
            )
        return TwitchCredentials(self.client_id, self.client_secret)

    def require_steam(self) -> SteamCredentials:
        if not self.steam_api_key or not self.steam_id:
            raise ConfigError("STEAM_API_KEY and STEAM_ID must be set for this command")
        return SteamCredentials(self.steam_api_key, self.steam_id)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        try:
            collector = CollectorConfig(
                top_n=_int_env(env, "SCOUT_TOP_N", DEFAULT_TOP_N),
                streams_max_pages=_int_env(
                    env, "SCOUT_STREAMS_MAX_PAGES", DEFAULT_STREAMS_MAX_PAGES
                ),
            )
        except ValueError as exc:
            # CollectorConfig validates ranges; re-raise as a config-level error.
            raise ConfigError(str(exc)) from exc
        return cls(
            db=env.get("SCOUT_DB", DEFAULT_DB),
            collector=collector,
            client_id=env.get("TWITCH_CLIENT_ID"),
            client_secret=env.get("TWITCH_CLIENT_SECRET"),
            turso_auth_token=env.get("TURSO_AUTH_TOKEN"),
            steam_api_key=env.get("STEAM_API_KEY"),
            steam_id=env.get("STEAM_ID"),
        )


def _int_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be an integer, got {raw!r}") from exc
