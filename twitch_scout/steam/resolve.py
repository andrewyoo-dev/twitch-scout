"""Resolve Steam game names to Twitch categories, conservatively.

Steam and Twitch often spell a game differently: separators ("X - Y" vs "X: Y"), edition
qualifiers ("- Complete Edition", "(2013)"), a subtitle only one side carries ("Tiny
Aquarium" vs "Tiny Aquarium: Social Fishkeeping"), accents, or an editorial choice
("Counter-Strike 2" is streamed under "Counter-Strike"). Tiers run in order, each only on
names the earlier tiers left unresolved, so a match is never overridden:

  1. alias:   a human-verified Steam -> Twitch mapping from ``aliases.toml``.
  2. exact:   the name with trademark marks stripped.
  3. variant: separators normalized ("X - Y" / "X : Y" -> "X: Y").
  4. edition: known edition/year/expansion qualifiers stripped.
  5. search:  Search Categories, accepting exactly one result that equals the name
              loosely (case, accents, punctuation ignored) or is the name plus a ":"
              subtitle.

Test/beta builds (PTS, Open Beta, Playtest, ...) are not looked up: they are not separate
games to stream. Truncating a name at its first separator was rejected: it reduces titles
to series names that match different categories ("Divinity", "Endzone"). A wrong match is
worse than a miss, because it samples the wrong category and shows an unowned game as
owned (docs/DECISIONS.md).
"""

from __future__ import annotations

import logging
import re
import tomllib
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from typing import Protocol

from twitch_scout.steam.client import OwnedSteamGame
from twitch_scout.twitch.models import HelixGame

logger = logging.getLogger(__name__)

# Coding standard 1: search is one API call per query, so bound it explicitly.
_MAX_SEARCHES = 150

_TRADEMARKS = dict.fromkeys(map(ord, "™®©"))
_TEST_BUILD = re.compile(
    r"\b(public test server|public testing|test server|pts|open beta|"
    r"closed technical beta|closed beta|beta|playtest)\s*$",
    re.IGNORECASE,
)
_EDITION = (
    r"(?:definitive|complete|special|anniversary|enhanced|reloaded|deluxe|gold|ultimate|"
    r"game of the year|goty|remastered)"
)
_TRAILING_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_SEPARATED_EDITION = re.compile(rf"\s*[-:]\s*{_EDITION}(?:\s+edition)?\s*$", re.IGNORECASE)
_BARE_EDITION = re.compile(rf"\s+{_EDITION}\s+edition\s*$", re.IGNORECASE)
# "Series: Title - Expansion" -> "Series: Title". Only when the base still carries a
# colon title, so "Endzone - A World Apart" is never cut down to a bare "Endzone".
_EXPANSION = re.compile(r"^(?P<base>[^:]+:[^-]+?)\s+-\s+[^:-]+$")


class SupportsGameLookup(Protocol):
    def get_games_by_name(self, names: list[str]) -> list[HelixGame]: ...

    def search_categories(self, query: str) -> list[HelixGame]: ...


@dataclass(frozen=True)
class Resolution:
    """Which owned appids resolved to a Twitch game, and how."""

    matches: dict[int, HelixGame] = field(default_factory=dict)
    via: dict[int, str] = field(default_factory=dict)  # appid -> tier (non-exact only)
    ignored: frozenset[int] = frozenset()  # test/beta builds, not looked up


def load_aliases() -> dict[str, str]:
    """Load the shipped Steam -> Twitch alias file (``aliases.toml``)."""
    raw = resources.files("twitch_scout.steam").joinpath("aliases.toml").read_text("utf-8")
    return parse_aliases(raw)


def parse_aliases(raw: str) -> dict[str, str]:
    """Parse alias TOML into a mapping keyed by normalized Steam name.

    The file is a trust boundary (hand-edited), so a malformed entry fails loud here
    rather than silently resolving nothing (coding standard 3). Raises ValueError
    (TOMLDecodeError is a ValueError).
    """
    table = tomllib.loads(raw).get("aliases", {})
    if not isinstance(table, dict):
        raise ValueError("aliases.toml: [aliases] must be a table")
    aliases: dict[str, str] = {}
    for steam_name, twitch_name in table.items():
        if not isinstance(twitch_name, str) or not twitch_name.strip() or not steam_name.strip():
            raise ValueError(f"aliases.toml: invalid entry {steam_name!r} = {twitch_name!r}")
        aliases[_alias_key(steam_name)] = twitch_name.strip()
    return aliases


def resolve_names(
    games: list[OwnedSteamGame],
    lookup: SupportsGameLookup,
    aliases: Mapping[str, str],
) -> Resolution:
    """Map owned appids to Twitch games through the tiers in the module docstring."""
    pending = {g.appid: g.name for g in games if not _TEST_BUILD.search(g.name)}
    ignored = frozenset(g.appid for g in games if g.appid not in pending)
    matches: dict[int, HelixGame] = {}
    via: dict[int, str] = {}

    def unresolved() -> dict[int, str]:
        return {appid: name for appid, name in pending.items() if appid not in matches}

    alias_candidates = {
        appid: [aliases[_alias_key(name)]]
        for appid, name in unresolved().items()
        if _alias_key(name) in aliases
    }
    _exact_tier(alias_candidates, lookup, "alias", matches, via)
    for appid, targets in alias_candidates.items():
        if appid not in matches:
            logger.warning("alias target %r not found on Twitch", targets[0])

    _exact_tier({a: [_clean(n)] for a, n in unresolved().items()}, lookup, "exact", matches, via)
    _exact_tier({a: _variants(n) for a, n in unresolved().items()}, lookup, "variant", matches, via)
    _exact_tier(
        {a: [s] for a, n in unresolved().items() if (s := _strip_edition(n))},
        lookup,
        "edition",
        matches,
        via,
    )
    _search_tier(unresolved(), lookup, matches, via)
    return Resolution(matches=matches, via=via, ignored=ignored)


def _exact_tier(
    candidates: dict[int, list[str]],
    lookup: SupportsGameLookup,
    tier: str,
    matches: dict[int, HelixGame],
    via: dict[int, str],
) -> None:
    """One batched Get Games call for every candidate name; first hit per appid wins."""
    names = list(dict.fromkeys(name for options in candidates.values() for name in options))
    if not names:
        return
    by_key = {_clean(game.name).casefold(): game for game in lookup.get_games_by_name(names)}
    for appid, options in candidates.items():
        hit = next(
            (by_key[k] for k in (_clean(o).casefold() for o in options) if k in by_key), None
        )
        if hit is not None:
            matches[appid] = hit
            if tier != "exact":
                via[appid] = tier


def _search_tier(
    unresolved: dict[int, str],
    lookup: SupportsGameLookup,
    matches: dict[int, HelixGame],
    via: dict[int, str],
) -> None:
    """Search Categories, accepting only a single verified result per game.

    A result is accepted if it equals the Steam name loosely, or else if it is the only
    result that is the Steam name plus a ":" subtitle. Acceptance always compares against
    the full (or edition-stripped) Steam name, never a truncated base, so a series-name
    category cannot be picked up.
    """
    searches = 0
    for appid, name in unresolved.items():
        titles = [t for t in (_clean(name), _strip_edition(name)) if t]
        queries = list(dict.fromkeys([*titles, _base_title(name)]))
        for query in queries:
            if searches >= _MAX_SEARCHES:
                logger.warning("search budget (%d) reached; leaving the rest unresolved", searches)
                return
            searches += 1
            verdict, game = _pick(lookup.search_categories(query), titles)
            if verdict == "match" and game is not None:
                matches[appid] = game
                via[appid] = "search"
                break
            if verdict == "ambiguous":
                logger.info("ambiguous search for %r; leaving it unresolved", name)
                break


def _pick(results: list[HelixGame], titles: list[str]) -> tuple[str, HelixGame | None]:
    """Choose one verified result: ("match", game), ("ambiguous", None) or ("none", None).

    A single loose-equal result wins outright (e.g. "Dark Souls" over "Dark Souls:
    Remastered"); otherwise exactly one "<title>: <subtitle>" result is required.
    """
    equal = {_loose(t) for t in titles}
    prefixes = tuple(_fold(t) + ":" for t in titles)
    exact = {g.id: g for g in results if _loose(g.name) in equal}
    if len(exact) == 1:
        return "match", next(iter(exact.values()))
    if len(exact) > 1:
        return "ambiguous", None
    subtitled = {g.id: g for g in results if _fold(g.name).startswith(prefixes)}
    if len(subtitled) == 1:
        return "match", next(iter(subtitled.values()))
    return ("ambiguous", None) if subtitled else ("none", None)


def _clean(name: str) -> str:
    """Drop trademark marks and collapse whitespace (case kept, for exact lookups)."""
    return " ".join(name.translate(_TRADEMARKS).split())


def _fold(name: str) -> str:
    """Accent- and case-insensitive form with separators unified to ": "."""
    decomposed = unicodedata.normalize("NFKD", _clean(name))
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return re.sub(r"\s*(?:\s-\s|:)\s*", ": ", plain)


def _loose(name: str) -> str:
    """Letters, digits and spaces only, for punctuation-insensitive equality."""
    return " ".join(re.sub(r"[^\w\s]", " ", _fold(name)).split())


def _alias_key(name: str) -> str:
    return _clean(name).casefold()


def _variants(name: str) -> list[str]:
    cleaned = _clean(name)
    options = [re.sub(r"\s+-\s+", ": ", cleaned), re.sub(r"\s+:\s*", ": ", cleaned)]
    return [v for v in dict.fromkeys(options) if v != cleaned]


def _strip_edition(name: str) -> str | None:
    """Remove edition/year/expansion qualifiers; None if nothing was removed."""
    cleaned = _clean(name)
    stripped = _TRAILING_PARENTHETICAL.sub("", cleaned)
    stripped = _SEPARATED_EDITION.sub("", stripped)
    stripped = _BARE_EDITION.sub("", stripped)
    expansion = _EXPANSION.match(stripped)
    if expansion:
        stripped = expansion.group("base")
    stripped = stripped.strip()
    return stripped if stripped and stripped != cleaned else None


def _base_title(name: str) -> str:
    """The part before the first separator, used only as a search QUERY, never to accept."""
    return re.split(r"\s*(?:\s-\s|:)\s*", _clean(name), maxsplit=1)[0]
