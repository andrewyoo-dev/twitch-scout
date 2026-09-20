"""Pydantic models validating Steam Web API responses at the trust boundary.

Same discipline as the Twitch models: validate the envelope, then each game item
individually so one malformed entry (a delisted app with no name, say) is logged and
skipped rather than failing the whole sync (coding standard 4).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# ResolveVanityURL success codes (Steam Web API): 1 = matched, 42 = no match.
VANITY_SUCCESS = 1


class VanityResolution(BaseModel):
    steamid: str | None = None
    success: int = 0


class VanityEnvelope(BaseModel):
    response: VanityResolution = Field(default_factory=VanityResolution)


class OwnedGamesData(BaseModel):
    """A private profile or an empty library returns ``{}`` here, hence the defaults."""

    game_count: int = 0
    games: list[dict[str, Any]] = Field(default_factory=list)


class OwnedGamesEnvelope(BaseModel):
    response: OwnedGamesData = Field(default_factory=OwnedGamesData)


class SteamGameItem(BaseModel):
    # include_appinfo=1 is required for name; an item without it is unusable, so a
    # missing/blank name fails validation and the item is skipped.
    appid: int = Field(gt=0)
    name: str = Field(min_length=1)
    playtime_forever: int = Field(ge=0, default=0)  # minutes, per the Steam API


def parse_owned_games(items: list[dict[str, Any]]) -> tuple[list[SteamGameItem], int]:
    """Validate each owned-game item; return the valid ones and the skipped count."""
    valid: list[SteamGameItem] = []
    skipped = 0
    for item in items:
        try:
            valid.append(SteamGameItem.model_validate(item))
        except ValidationError as exc:
            skipped += 1
            logger.warning("dropping invalid Steam game item: %s", exc.errors())
    return valid, skipped
