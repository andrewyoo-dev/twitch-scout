"""Tests for Helix boundary validation.

The point is tolerance to real-world mess: one malformed stream (missing
viewer_count, blank user_id) is dropped and counted, not allowed to crash the whole
sample. See handoff section 9.
"""

from __future__ import annotations

from twitch_scout.twitch.models import (
    HelixEnvelope,
    TokenResponse,
    parse_games,
    parse_streams,
)


def test_envelope_defaults_pagination_when_absent() -> None:
    env = HelixEnvelope.model_validate({"data": []})
    assert env.data == []
    assert env.pagination.cursor is None


def test_parse_games_valid() -> None:
    items = [
        {"id": "509658", "name": "Just Chatting", "box_art_url": "x"},
        {"id": "12345", "name": "Job Simulator"},
    ]
    games, skipped = parse_games(items)
    assert skipped == 0
    assert [g.id for g in games] == ["509658", "12345"]


def test_parse_games_skips_blank_id() -> None:
    # A null/blank game_id occurs in practice; drop it, keep the rest.
    items = [
        {"id": "", "name": "Broken"},
        {"id": "12345", "name": "Job Simulator"},
    ]
    games, skipped = parse_games(items)
    assert skipped == 1
    assert [g.id for g in games] == ["12345"]


def test_parse_streams_valid() -> None:
    items = [
        {"user_id": "1", "game_id": "12345", "viewer_count": 100, "language": "en"},
        {"user_id": "2", "game_id": "12345", "viewer_count": 5},
    ]
    streams, skipped = parse_streams(items)
    assert skipped == 0
    assert [s.viewer_count for s in streams] == [100, 5]
    assert streams[1].language == ""  # defaulted


def test_parse_streams_skips_missing_viewer_count() -> None:
    items = [
        {"user_id": "1", "game_id": "12345"},  # no viewer_count
        {"user_id": "2", "game_id": "12345", "viewer_count": 42},
    ]
    streams, skipped = parse_streams(items)
    assert skipped == 1
    assert [s.viewer_count for s in streams] == [42]


def test_parse_streams_skips_negative_viewer_count_and_blank_user() -> None:
    items = [
        {"user_id": "1", "game_id": "12345", "viewer_count": -3},
        {"user_id": "", "game_id": "12345", "viewer_count": 10},
        {"user_id": "3", "game_id": "12345", "viewer_count": 10},
    ]
    streams, skipped = parse_streams(items)
    assert skipped == 2
    assert [s.user_id for s in streams] == ["3"]


def test_stream_allows_empty_game_id() -> None:
    # Empty game_id is valid at the model level (uncategorized); the client filters
    # it out of a specific category's aggregate, not the validator.
    items = [{"user_id": "1", "game_id": "", "viewer_count": 10}]
    streams, skipped = parse_streams(items)
    assert skipped == 0
    assert streams[0].game_id == ""


def test_token_response_valid() -> None:
    tok = TokenResponse.model_validate(
        {"access_token": "abc", "expires_in": 5000000, "token_type": "bearer"}
    )
    assert tok.access_token == "abc"
    assert tok.expires_in == 5000000


def test_token_response_rejects_empty_token() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TokenResponse.model_validate({"access_token": "", "expires_in": 100})
