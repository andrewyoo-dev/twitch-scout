"""Pydantic models validating Helix responses at the trust boundary.

Helix returns real-world mess: streams missing ``viewer_count``, uncategorized
streams with an empty ``game_id``, occasionally renamed games (handoff section 9).
The envelope is validated as a whole, then each item is validated individually so a
single malformed item is logged and skipped rather than failing the entire sample.
Skipping is a deliberate, logged degradation — not a swallowed error (standard 4).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


class Pagination(BaseModel):
    cursor: str | None = None


class HelixEnvelope(BaseModel):
    """The outer shape shared by list endpoints: a data array plus a cursor."""

    data: list[dict[str, Any]]
    pagination: Pagination = Field(default_factory=Pagination)


class HelixGame(BaseModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class HelixStream(BaseModel):
    user_id: str = Field(min_length=1)
    # game_id may be an empty string for an uncategorized stream; the aggregator
    # drops those rather than treating empty as a category.
    game_id: str = ""
    game_name: str = ""
    viewer_count: int = Field(ge=0)
    language: str = ""


class TokenResponse(BaseModel):
    access_token: str = Field(min_length=1)
    expires_in: int = Field(gt=0)
    token_type: str = ""


def _parse_items[ItemT: BaseModel](
    items: list[dict[str, Any]], model: type[ItemT]
) -> tuple[list[ItemT], int]:
    """Validate each item; return the valid ones and the count skipped as invalid."""
    valid: list[ItemT] = []
    skipped = 0
    for item in items:
        try:
            valid.append(model.model_validate(item))
        except ValidationError as exc:
            skipped += 1
            logger.warning("dropping invalid %s item: %s", model.__name__, exc.errors())
    return valid, skipped


def parse_games(items: list[dict[str, Any]]) -> tuple[list[HelixGame], int]:
    return _parse_items(items, HelixGame)


def parse_streams(items: list[dict[str, Any]]) -> tuple[list[HelixStream], int]:
    return _parse_items(items, HelixStream)
