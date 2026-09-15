"""Read-only Twitch Helix client.

Auth is an App Access Token (client credentials); no user login. The client owns
four cross-cutting concerns the collector must not have to think about:

  * Auth — fetch, cache, and refresh the token (including on a 401).
  * Rate limiting — pace requests through a token bucket, back off on a 429.
  * Pagination — walk cursors up to a hard page cap (coding standard 1).
  * Boundary validation — parse every response through the models, skipping
    malformed items rather than crashing the sample (standards 3, 4).

Every dependency that is nondeterministic or does I/O — the HTTP client, the clock,
the bucket, sleep — is injected, so the whole thing is testable against an
``httpx.MockTransport`` with no network and no real waiting (standard 9).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx

from twitch_scout.clock import Clock, SystemClock
from twitch_scout.twitch.models import (
    HelixEnvelope,
    HelixGame,
    HelixStream,
    TokenResponse,
    parse_games,
    parse_streams,
)
from twitch_scout.twitch.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

TOKEN_URL = "https://id.twitch.tv/oauth2/token"
API_BASE = "https://api.twitch.tv/helix"
TOP_GAMES_URL = f"{API_BASE}/games/top"
STREAMS_URL = f"{API_BASE}/streams"

PAGE_SIZE = 100  # Helix maximum items per page
_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_MAX_RETRIES = 3
# Refresh a little before the token actually expires so an in-flight sample never
# races the expiry. App tokens last ~60 days, so this margin is negligible.
_TOKEN_REFRESH_MARGIN_S = 300
_DEFAULT_RETRY_AFTER_S = 1.0

_HTTP_OK = 200
_HTTP_UNAUTHORIZED = 401
_HTTP_TOO_MANY_REQUESTS = 429


class TwitchError(Exception):
    """Base class for every error this client raises."""


class TwitchAuthError(TwitchError):
    """Failed to obtain an app access token."""


class TwitchApiError(TwitchError):
    """A Helix request failed (bad status, network error, or exhausted retries)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class StreamsResult:
    """The streams sampled for one game, and whether the page cap was hit.

    ``truncated`` is True when more pages existed beyond the cap — the counts are a
    floor, not the exact total. Small categories (the streamer's targets) fit in one
    page and come back exact; only giant categories he would never compete in get
    truncated.
    """

    streams: list[HelixStream]
    truncated: bool

    @property
    def channels(self) -> int:
        return len(self.streams)

    @property
    def viewers(self) -> int:
        return sum(s.viewer_count for s in self.streams)


class HelixClient:
    def __init__(  # noqa: PLR0913 -- injected collaborators (http, clock, bucket, sleep) are explicit by design
        self,
        client_id: str,
        client_secret: str,
        *,
        http: httpx.Client,
        clock: Clock,
        bucket: TokenBucket,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("client_id and client_secret are required")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http
        self._clock = clock
        self._bucket = bucket
        self._sleep = sleep
        self._max_retries = max_retries
        self._token: str | None = None
        self._expires_at: datetime | None = None

    @classmethod
    def create(
        cls,
        client_id: str,
        client_secret: str,
        *,
        clock: Clock | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> HelixClient:
        """Build a client wired to the real network and a Helix-tuned bucket."""
        return cls(
            client_id,
            client_secret,
            http=httpx.Client(timeout=timeout),
            clock=clock or SystemClock(),
            bucket=TokenBucket.for_helix(),
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> HelixClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- public endpoints ---

    def get_top_games(self, limit: int) -> list[HelixGame]:
        """Games ordered by current viewers, up to ``limit``. Pagination is bounded
        by the number of pages ``limit`` implies, so an endless cursor cannot loop."""
        if limit <= 0:
            raise ValueError("limit must be > 0")
        max_pages = -(-limit // PAGE_SIZE)  # ceil division
        games: list[HelixGame] = []
        cursor: str | None = None
        for _ in range(max_pages):
            envelope = self._get_page(TOP_GAMES_URL, self._page_params(cursor))
            parsed, _skipped = parse_games(envelope.data)
            games.extend(parsed)
            cursor = envelope.pagination.cursor
            if cursor is None or len(games) >= limit:
                break
        return games[:limit]

    def get_streams(self, game_id: str, *, max_pages: int) -> StreamsResult:
        """Live streams for one game, paginated up to ``max_pages`` (each up to 100
        streams, viewer-count descending). If more pages remain past the cap the
        result is flagged truncated."""
        if not game_id:
            raise ValueError("game_id is required")
        if max_pages <= 0:
            raise ValueError("max_pages must be > 0")
        streams: list[HelixStream] = []
        cursor: str | None = None
        truncated = False
        for _ in range(max_pages):
            params = {"game_id": game_id, **self._page_params(cursor)}
            envelope = self._get_page(STREAMS_URL, params)
            parsed, _skipped = parse_streams(envelope.data)
            # Uncategorized streams (empty game_id) are not part of this category.
            streams.extend(s for s in parsed if s.game_id == game_id)
            cursor = envelope.pagination.cursor
            if cursor is None:
                break
        else:
            # Loop hit the cap without exhausting the cursor: more pages exist.
            truncated = cursor is not None
        return StreamsResult(streams=streams, truncated=truncated)

    # --- request plumbing ---

    @staticmethod
    def _page_params(cursor: str | None) -> dict[str, str | int]:
        params: dict[str, str | int] = {"first": PAGE_SIZE}
        if cursor is not None:
            params["after"] = cursor
        return params

    def _get_page(self, url: str, params: dict[str, str | int]) -> HelixEnvelope:
        payload = self._get(url, params)
        return HelixEnvelope.model_validate(payload)

    def _get(self, url: str, params: dict[str, str | int]) -> object:
        # Bounded retry loop: initial attempt plus max_retries (standard 1).
        for _ in range(self._max_retries + 1):
            self._ensure_token()
            self._bucket.acquire()
            try:
                response = self._http.get(url, params=params, headers=self._auth_headers())
            except httpx.HTTPError as exc:
                # Timeout / connection failure: surface it so the collector logs and
                # skips this sample rather than writing a partial row (standard 2).
                raise TwitchApiError(-1, f"request failed: {exc}") from exc

            if response.status_code == _HTTP_OK:
                return response.json()
            if response.status_code == _HTTP_TOO_MANY_REQUESTS:
                self._sleep(self._retry_after(response))
                continue
            if response.status_code == _HTTP_UNAUTHORIZED:
                self._token = None  # force a refresh, then retry
                continue
            raise TwitchApiError(response.status_code, response.text)
        raise TwitchApiError(-1, f"exhausted {self._max_retries} retries for {url}")

    def _auth_headers(self) -> dict[str, str]:
        if self._token is None:
            raise TwitchAuthError("no token available")
        return {"Client-Id": self._client_id, "Authorization": f"Bearer {self._token}"}

    def _ensure_token(self) -> None:
        now = self._clock.now()
        if self._token is None or self._expires_at is None or now >= self._expires_at:
            self._fetch_token()

    def _fetch_token(self) -> None:
        try:
            response = self._http.post(
                TOKEN_URL,
                data={
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "grant_type": "client_credentials",
                },
            )
        except httpx.HTTPError as exc:
            raise TwitchAuthError(f"token request failed: {exc}") from exc
        if response.status_code != _HTTP_OK:
            raise TwitchAuthError(f"token endpoint returned HTTP {response.status_code}")
        token = TokenResponse.model_validate(response.json())
        margin = min(_TOKEN_REFRESH_MARGIN_S, token.expires_in // 2)
        self._token = token.access_token
        self._expires_at = self._clock.now() + timedelta(seconds=token.expires_in - margin)
        logger.debug("obtained app token, expires in %ss", token.expires_in)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        raw = response.headers.get("Retry-After")
        if raw is None:
            return _DEFAULT_RETRY_AFTER_S
        try:
            return max(0.0, float(raw))
        except ValueError:
            # Malformed header: fall back rather than crash the sample (standard 4).
            logger.warning("unparseable Retry-After header: %r", raw)
            return _DEFAULT_RETRY_AFTER_S
