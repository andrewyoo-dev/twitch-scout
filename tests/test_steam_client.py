"""Tests for the Steam Web API client, driven by httpx.MockTransport (no network)."""

from __future__ import annotations

import httpx
import pytest

from twitch_scout.steam.client import (
    OWNED_GAMES_URL,
    RESOLVE_VANITY_URL,
    OwnedSteamGame,
    SteamApiError,
    SteamAuthError,
    SteamClient,
)


class SteamMock:
    def __init__(self) -> None:
        self.vanity_steamid: str | None = "76561190000000000"
        self.owned: list[dict[str, object]] | None = [
            {"appid": 1, "name": "Cozy Cove", "playtime_forever": 600},
            {"appid": 2, "name": "Job Simulator", "playtime_forever": 30},
        ]
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={})
        if request.url.path == httpx.URL(RESOLVE_VANITY_URL).path:
            if self.vanity_steamid is None:
                return httpx.Response(200, json={"response": {"success": 42}})
            return httpx.Response(
                200, json={"response": {"steamid": self.vanity_steamid, "success": 1}}
            )
        if request.url.path == httpx.URL(OWNED_GAMES_URL).path:
            if self.owned is None:
                return httpx.Response(200, json={"response": {}})  # private profile
            return httpx.Response(
                200, json={"response": {"game_count": len(self.owned), "games": self.owned}}
            )
        return httpx.Response(404, json={})


def _client(mock: SteamMock) -> SteamClient:
    http = httpx.Client(transport=httpx.MockTransport(mock.handler))
    return SteamClient("key", http=http)


def test_missing_api_key_rejected() -> None:
    with pytest.raises(ValueError, match="api_key is required"):
        SteamClient("", http=httpx.Client())


def test_resolve_vanity_name() -> None:
    client = _client(SteamMock())
    assert client.resolve_steam_id("kamagui") == "76561190000000000"


def test_resolve_passes_through_a_steamid64() -> None:
    mock = SteamMock()
    mock.vanity_steamid = None  # would fail if a request were made
    client = _client(mock)
    # A 17-digit id is already a steamid and must not hit ResolveVanityURL.
    assert client.resolve_steam_id("76561190000000000") == "76561190000000000"


def test_resolve_unmatched_vanity_raises() -> None:
    mock = SteamMock()
    mock.vanity_steamid = None
    client = _client(mock)
    with pytest.raises(SteamApiError, match="could not resolve"):
        client.resolve_steam_id("no-such-user")


def test_get_owned_games_parses_playtime() -> None:
    client = _client(SteamMock())
    games = client.get_owned_games("76561190000000000")
    assert games == [
        OwnedSteamGame(appid=1, name="Cozy Cove", playtime_minutes=600),
        OwnedSteamGame(appid=2, name="Job Simulator", playtime_minutes=30),
    ]


def test_get_owned_games_skips_malformed_items() -> None:
    mock = SteamMock()
    mock.owned = [
        {"appid": 1, "name": "Cozy Cove", "playtime_forever": 600},
        {"appid": 2, "playtime_forever": 30},  # no name (missing include_appinfo)
    ]
    client = _client(mock)
    games = client.get_owned_games("76561190000000000")
    assert [g.appid for g in games] == [1]


def test_get_owned_games_private_profile_is_empty() -> None:
    mock = SteamMock()
    mock.owned = None
    client = _client(mock)
    assert client.get_owned_games("76561190000000000") == []


def test_auth_error_on_forbidden() -> None:
    mock = SteamMock()
    mock.status = 403
    client = _client(mock)
    with pytest.raises(SteamAuthError, match="STEAM_API_KEY"):
        client.get_owned_games("76561190000000000")


def test_api_key_never_appears_in_raised_error() -> None:
    mock = SteamMock()
    mock.status = 500
    http = httpx.Client(transport=httpx.MockTransport(mock.handler))
    client = SteamClient("super-secret-key", http=http)
    with pytest.raises(SteamApiError) as excinfo:
        client.get_owned_games("76561190000000000")
    assert "super-secret-key" not in str(excinfo.value)


def test_client_is_a_context_manager() -> None:
    with _client(SteamMock()) as client:
        assert client.resolve_steam_id("kamagui") == "76561190000000000"
