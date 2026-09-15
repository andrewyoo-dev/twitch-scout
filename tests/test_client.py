"""Tests for the Helix client, driven by httpx.MockTransport (no network).

A stateful mock serves the token endpoint plus paginated games/streams, and can be
told to emit a queue of 429/401 statuses before serving normally, so retry and
refresh behaviour is exercised deterministically.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from twitch_scout.twitch.client import (
    HelixClient,
    TwitchApiError,
    TwitchAuthError,
)
from twitch_scout.twitch.ratelimit import TokenBucket

FIXED = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


class StubClock:
    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t


class TwitchMock:
    def __init__(self) -> None:
        self.token_calls = 0
        self.token_status = 200
        self.games: list[tuple[list[dict[str, object]], str | None]] = [([], None)]
        self.streams: dict[str, list[tuple[list[dict[str, object]], str | None]]] = {}
        self.status_queue: deque[int] = deque()
        self.retry_after = "2"

    @staticmethod
    def _page(
        pages: list[tuple[list[dict[str, object]], str | None]], cursor: str | None
    ) -> tuple[list[dict[str, object]], str | None]:
        idx = 0 if cursor is None else int(cursor)
        return pages[idx]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            self.token_calls += 1
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "bad"})
            return httpx.Response(
                200,
                json={
                    "access_token": f"tok{self.token_calls}",
                    "expires_in": 5_000_000,
                    "token_type": "bearer",
                },
            )
        if self.status_queue:
            status = self.status_queue.popleft()
            if status == 429:
                return httpx.Response(429, headers={"Retry-After": self.retry_after}, json={})
            if status == 401:
                return httpx.Response(401, json={})
        if request.url.path.endswith("/games/top"):
            data, nxt = self._page(self.games, request.url.params.get("after"))
            return httpx.Response(200, json=_envelope(data, nxt))
        if request.url.path.endswith("/streams"):
            gid = request.url.params.get("game_id")
            assert gid is not None
            data, nxt = self._page(self.streams[gid], request.url.params.get("after"))
            return httpx.Response(200, json=_envelope(data, nxt))
        return httpx.Response(404, json={})


def _envelope(data: list[dict[str, object]], cursor: str | None) -> dict[str, object]:
    pagination = {"cursor": cursor} if cursor else {}
    return {"data": data, "pagination": pagination}


def _games(n: int, start: int = 0) -> list[dict[str, object]]:
    return [{"id": str(i), "name": f"Game {i}"} for i in range(start, start + n)]


def _streams(
    viewers: list[int], game_id: str = "12345", start_uid: int = 0
) -> list[dict[str, object]]:
    return [
        {"user_id": str(start_uid + i), "game_id": game_id, "viewer_count": v}
        for i, v in enumerate(viewers)
    ]


def _client(
    mock: TwitchMock,
    *,
    clock: StubClock | None = None,
    max_retries: int = 3,
) -> tuple[HelixClient, list[float]]:
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)

    http = httpx.Client(transport=httpx.MockTransport(mock.handler))
    bucket = TokenBucket.for_helix(sleep=lambda _: None)
    client = HelixClient(
        "cid",
        "secret",
        http=http,
        clock=clock or StubClock(FIXED),
        bucket=bucket,
        sleep=sleep,
        max_retries=max_retries,
    )
    return client, slept


# --- construction ---


@pytest.mark.parametrize(("cid", "secret"), [("", "s"), ("c", "")])
def test_missing_credentials_rejected(cid: str, secret: str) -> None:
    with pytest.raises(ValueError, match="required"):
        HelixClient(
            cid,
            secret,
            http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
            clock=StubClock(FIXED),
            bucket=TokenBucket.for_helix(),
        )


# --- top games ---


def test_get_top_games_single_page() -> None:
    mock = TwitchMock()
    mock.games = [(_games(2), None)]
    client, _ = _client(mock)
    games = client.get_top_games(100)
    assert [g.id for g in games] == ["0", "1"]
    assert mock.token_calls == 1  # token fetched once


def test_get_top_games_paginates_up_to_limit() -> None:
    mock = TwitchMock()
    mock.games = [(_games(100, 0), "1"), (_games(100, 100), "2"), (_games(100, 200), None)]
    client, _ = _client(mock)
    games = client.get_top_games(250)
    assert len(games) == 250
    assert mock.token_calls == 1  # cached across pages


def test_get_top_games_stops_at_page_cap_before_exhausting_cursor() -> None:
    mock = TwitchMock()
    # One page of 100 with a cursor pointing further, but limit=100 => one page only.
    mock.games = [(_games(100, 0), "1"), (_games(100, 100), None)]
    client, _ = _client(mock)
    games = client.get_top_games(100)
    assert len(games) == 100


def test_get_top_games_rejects_bad_limit() -> None:
    client, _ = _client(TwitchMock())
    with pytest.raises(ValueError, match="limit must be > 0"):
        client.get_top_games(0)


# --- streams ---


def test_get_streams_single_page_not_truncated() -> None:
    mock = TwitchMock()
    mock.streams = {"12345": [(_streams([100, 50, 5]), None)]}
    client, _ = _client(mock)
    result = client.get_streams("12345", max_pages=3)
    assert result.channels == 3
    assert result.viewers == 155
    assert result.truncated is False


def test_get_streams_truncates_when_pages_exceed_cap() -> None:
    mock = TwitchMock()
    mock.streams = {
        "12345": [
            (_streams([10] * 100, start_uid=0), "1"),
            (_streams([10] * 100, start_uid=100), "2"),  # cursor still present at cap
        ]
    }
    client, _ = _client(mock)
    result = client.get_streams("12345", max_pages=2)
    assert result.channels == 200
    assert result.truncated is True


def test_get_streams_filters_uncategorized() -> None:
    mock = TwitchMock()
    page = _streams([10, 20], game_id="12345")
    page.append({"user_id": "99", "game_id": "", "viewer_count": 1000})  # uncategorized
    mock.streams = {"12345": [(page, None)]}
    client, _ = _client(mock)
    result = client.get_streams("12345", max_pages=1)
    assert result.channels == 2  # the empty-game_id stream dropped
    assert result.viewers == 30


def test_get_streams_rejects_bad_args() -> None:
    client, _ = _client(TwitchMock())
    with pytest.raises(ValueError, match="game_id is required"):
        client.get_streams("", max_pages=1)
    with pytest.raises(ValueError, match="max_pages must be > 0"):
        client.get_streams("12345", max_pages=0)


# --- retries and auth ---


def test_429_is_retried_after_backoff() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    mock.status_queue = deque([429])  # first API GET => 429, then served
    client, slept = _client(mock)
    games = client.get_top_games(100)
    assert len(games) == 1
    assert slept == [pytest.approx(2.0)]  # Retry-After honored


def test_401_triggers_token_refresh_and_retry() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    mock.status_queue = deque([401])  # first API GET => 401
    client, _ = _client(mock)
    games = client.get_top_games(100)
    assert len(games) == 1
    assert mock.token_calls == 2  # refetched after the 401


def test_exhausted_retries_raises() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    mock.status_queue = deque([429, 429, 429, 429])  # more 429s than retries
    client, _ = _client(mock, max_retries=2)  # 3 attempts total
    with pytest.raises(TwitchApiError, match="exhausted"):
        client.get_top_games(100)


def test_non_retriable_status_raises_immediately() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "t", "expires_in": 5000000, "token_type": "bearer"}
            )
        return httpx.Response(500, text="server error")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = HelixClient(
        "c", "s", http=http, clock=StubClock(FIXED), bucket=TokenBucket.for_helix()
    )
    with pytest.raises(TwitchApiError) as exc:
        client.get_top_games(100)
    assert exc.value.status == 500


def test_network_error_becomes_api_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "id.twitch.tv":
            return httpx.Response(
                200, json={"access_token": "t", "expires_in": 5000000, "token_type": "bearer"}
            )
        raise httpx.ConnectError("boom")

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = HelixClient(
        "c", "s", http=http, clock=StubClock(FIXED), bucket=TokenBucket.for_helix()
    )
    with pytest.raises(TwitchApiError, match="request failed"):
        client.get_top_games(100)


def test_token_is_cached_across_calls() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    client, _ = _client(mock)
    client.get_top_games(100)
    client.get_top_games(100)
    assert mock.token_calls == 1


def test_token_refreshes_after_expiry() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    clock = StubClock(FIXED)
    client, _ = _client(mock, clock=clock)
    client.get_top_games(100)
    assert mock.token_calls == 1
    clock.t = FIXED + timedelta(seconds=5_000_000)  # past expiry
    client.get_top_games(100)
    assert mock.token_calls == 2


def test_token_endpoint_failure_raises_auth_error() -> None:
    mock = TwitchMock()
    mock.token_status = 403
    client, _ = _client(mock)
    with pytest.raises(TwitchAuthError, match="HTTP 403"):
        client.get_top_games(100)


def test_client_is_a_context_manager() -> None:
    mock = TwitchMock()
    mock.games = [(_games(1), None)]
    client, _ = _client(mock)
    with client as c:
        assert c.get_top_games(100)
