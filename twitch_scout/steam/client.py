"""Read-only Steam Web API client: resolve a profile id and list owned games.

Only two endpoints are needed for the owned-games candidate source:

  * ``ResolveVanityURL`` turns a vanity name (``kamagui``) into a 64-bit steamid.
  * ``GetOwnedGames`` lists the library with names and playtime.

The Steam Web API takes its key as a query parameter (it has no header form), so the
key rides in the query string; the client never logs a URL, to keep the key out of
logs. The HTTP client is injected so tests run against an ``httpx.MockTransport`` with
no network (coding standard 9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from twitch_scout.steam.models import (
    VANITY_SUCCESS,
    OwnedGamesEnvelope,
    VanityEnvelope,
    parse_owned_games,
)

logger = logging.getLogger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
RESOLVE_VANITY_URL = f"{STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"
OWNED_GAMES_URL = f"{STEAM_API_BASE}/IPlayerService/GetOwnedGames/v1/"

_DEFAULT_TIMEOUT_S = 10.0
_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
# A 64-bit steamid is 17 digits; anything else is treated as a vanity name.
_STEAMID64_LEN = 17


class SteamError(Exception):
    """Base class for every error this client raises."""


class SteamAuthError(SteamError):
    """The Steam Web API key is missing, invalid, or lacks access."""


class SteamApiError(SteamError):
    """A Steam request failed (bad status, network error, or unresolvable input)."""


@dataclass(frozen=True)
class OwnedSteamGame:
    """One owned game: its appid, display name, and total playtime in minutes."""

    appid: int
    name: str
    playtime_minutes: int


class SteamClient:
    def __init__(self, api_key: str, *, http: httpx.Client) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._http = http

    @classmethod
    def create(cls, api_key: str, *, timeout: float = _DEFAULT_TIMEOUT_S) -> SteamClient:
        return cls(api_key, http=httpx.Client(timeout=timeout))

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SteamClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- public endpoints ---

    def resolve_steam_id(self, id_or_vanity: str) -> str:
        """Return a 64-bit steamid for a raw id or a vanity name.

        A 17-digit numeric string is already a steamid and is returned unchanged; any
        other value is resolved via ResolveVanityURL.
        """
        candidate = id_or_vanity.strip()
        if not candidate:
            raise ValueError("steam id or vanity name is required")
        if candidate.isdigit() and len(candidate) == _STEAMID64_LEN:
            return candidate
        payload = self._get(RESOLVE_VANITY_URL, {"vanityurl": candidate})
        parsed = VanityEnvelope.model_validate(payload)
        if parsed.response.success != VANITY_SUCCESS or not parsed.response.steamid:
            raise SteamApiError(f"could not resolve Steam vanity name {candidate!r}")
        return parsed.response.steamid

    def get_owned_games(self, steam_id: str) -> list[OwnedSteamGame]:
        """List the account's owned games with names and playtime.

        Requires the profile's game details to be public; a private profile comes back
        empty (an empty list), which the caller reports rather than treating as an error.
        """
        if not steam_id:
            raise ValueError("steam_id is required")
        payload = self._get(
            OWNED_GAMES_URL,
            {"steamid": steam_id, "include_appinfo": 1, "include_played_free_games": 1},
        )
        envelope = OwnedGamesEnvelope.model_validate(payload)
        parsed, skipped = parse_owned_games(envelope.response.games)
        if skipped:
            logger.warning("skipped %d malformed Steam game item(s)", skipped)
        return [
            OwnedSteamGame(appid=g.appid, name=g.name, playtime_minutes=g.playtime_forever)
            for g in parsed
        ]

    # --- request plumbing ---

    def _get(self, url: str, params: dict[str, str | int]) -> object:
        request_params = {**params, "key": self._api_key, "format": "json"}
        try:
            response = self._http.get(url, params=request_params)
        except httpx.HTTPError as exc:
            # Do not include the URL: it carries the key. Name the endpoint instead.
            raise SteamApiError(f"Steam request to {_endpoint_name(url)} failed: {exc}") from exc
        if response.status_code == _HTTP_OK:
            return response.json()
        if response.status_code in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
            raise SteamAuthError(
                f"Steam API returned HTTP {response.status_code} (check STEAM_API_KEY)"
            )
        raise SteamApiError(f"Steam API returned HTTP {response.status_code}")


def _endpoint_name(url: str) -> str:
    return url.rstrip("/").rsplit("/", 2)[-2] if "/" in url else url
