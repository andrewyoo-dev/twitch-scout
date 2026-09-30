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
    return resolve_titles({g.appid: g.name for g in games}, lookup, aliases)


def resolve_titles(
    titles: Mapping[int, str],
    lookup: SupportsGameLookup,
    aliases: Mapping[str, str],
) -> Resolution:
    """Resolve arbitrary keyed titles (Steam appids, or any caller key) through the
    same tiers."""
    pending = {key: name for key, name in titles.items() if not _TEST_BUILD.search(name)}
    ignored = frozenset(key for key in titles if key not in pending)
    matches: dict[int, HelixGame] = {}
    via: dict[int, str] = {}
    budget = _SearchBudget(_MAX_SEARCHES)

    def unresolved() -> dict[int, str]:
        return {appid: name for appid, name in pending.items() if appid not in matches}

    alias_candidates = {
        appid: [aliases[_alias_key(name)]]
        for appid, name in unresolved().items()
        if _alias_key(name) in aliases
    }
    _exact_tier(alias_candidates, lookup, "alias", matches, via, budget=budget)
    for appid, targets in alias_candidates.items():
        if appid not in matches:
            logger.warning("alias target %r not found on Twitch", targets[0])

    exact = {a: [_clean(n)] for a, n in unresolved().items()}
    _exact_tier(exact, lookup, "exact", matches, via, budget=budget)
    variants = {a: _variants(n) for a, n in unresolved().items()}
    _exact_tier(variants, lookup, "variant", matches, via, budget=budget)
    editions = {a: [s] for a, n in unresolved().items() if (s := _strip_edition(n))}
    _exact_tier(editions, lookup, "edition", matches, via, budget=budget)
    _search_tier(unresolved(), lookup, matches, via, budget)
    return Resolution(matches=matches, via=via, ignored=ignored)


class _SearchBudget:
    """Search calls shared by every tier of one resolution (coding standard 1)."""

    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _exact_tier(  # noqa: PLR0913 -- tier inputs plus the shared outputs and search budget
    candidates: dict[int, list[str]],
    lookup: SupportsGameLookup,
    tier: str,
    matches: dict[int, HelixGame],
    via: dict[int, str],
    *,
    budget: _SearchBudget,
) -> None:
    """One batched Get Games call for every candidate name; first verified hit wins."""
    names = list(dict.fromkeys(name for options in candidates.values() for name in options))
    if not names:
        return
    by_key = {_clean(game.name).casefold(): game for game in lookup.get_games_by_name(names)}
    for appid, options in candidates.items():
        for option in options:
            hit = by_key.get(_clean(option).casefold())
            if hit is not None and _clean(hit.name) != _clean(option):
                hit = _confirm_case(option, hit, lookup, budget)
            if hit is not None:
                matches[appid] = hit
                if tier != "exact":
                    via[appid] = tier
                break


def _confirm_case(
    name: str, hit: HelixGame, lookup: SupportsGameLookup, budget: _SearchBudget
) -> HelixGame | None:
    """Verify a Get Games hit whose name differs from the query only by letter case.

    Get Games matches names case-insensitively and, when two categories differ only by
    case, can return the other one ("Dressmaker" came back as the unrelated
    "DressMaker"). Search for the name: if the hit is the only category with that
    spelling it is accepted; otherwise the exact-case category wins, and with no
    exact-case one the match is refused as ambiguous. Without search budget left the
    hit is refused too, since a wrong match is worse than a miss.
    """
    if not budget.take():
        logger.warning("search budget exhausted; not confirming %r -> %r", name, hit.name)
        return None
    key = _clean(name).casefold()
    same = {g.id: g for g in lookup.search_categories(name) if _clean(g.name).casefold() == key}
    same.setdefault(hit.id, hit)
    if len(same) == 1:
        return hit
    exact_case = [g for g in same.values() if _clean(g.name) == _clean(name)]
    if len(exact_case) == 1:
        return exact_case[0]
    logger.info(
        "%r matches categories differing only by case: %s",
        name,
        sorted(g.name for g in same.values()),
    )
    return None


def _search_tier(
    unresolved: dict[int, str],
    lookup: SupportsGameLookup,
    matches: dict[int, HelixGame],
    via: dict[int, str],
    budget: _SearchBudget,
) -> None:
    """Search Categories, accepting only a single verified result per game.

    A result is accepted if it equals the Steam name loosely, or else if it is the only
    result that is the Steam name plus a ":" subtitle. Acceptance always compares against
    the full (or edition-stripped) Steam name, never a truncated base, so a series-name
    category cannot be picked up.
    """
    for appid, name in unresolved.items():
        titles = [t for t in (_clean(name), _strip_edition(name)) if t]
        queries = list(dict.fromkeys([*titles, _base_title(name)]))
        for query in queries:
            if not budget.take():
                logger.warning(
                    "search budget (%d) reached; leaving the rest unresolved", _MAX_SEARCHES
                )
                return
            verdict, found = _pick(lookup.search_categories(query), titles)
            if verdict == "ambiguous":
                found = _canonical(found, lookup)
                verdict = "match" if found else "ambiguous"
            if verdict == "match":
                matches[appid] = found[0]
                via[appid] = "search"
                break
            if verdict == "ambiguous":
                logger.info("ambiguous search for %r; leaving it unresolved", name)
                break


def _pick(results: list[HelixGame], titles: list[str]) -> tuple[str, list[HelixGame]]:
    """Classify verified results: ("match", [game]), ("ambiguous", games) or ("none", []).

    A single loose-equal result wins outright (e.g. "Dark Souls" over "Dark Souls:
    Remastered"); otherwise exactly one "<title>: <subtitle>" result is required.
    """
    equal = {_loose(t) for t in titles}
    prefixes = tuple(_fold(t) + ":" for t in titles)
    exact = list({g.id: g for g in results if _loose(g.name) in equal}.values())
    if len(exact) == 1:
        return "match", exact
    if exact:
        return "ambiguous", exact
    subtitled = list({g.id: g for g in results if _fold(g.name).startswith(prefixes)}.values())
    if len(subtitled) == 1:
        return "match", subtitled
    return ("ambiguous", subtitled) if subtitled else ("none", [])


def _canonical(tied: list[HelixGame], lookup: SupportsGameLookup) -> list[HelixGame]:
    """Break a tie between categories that share one exact name, via Get Games.

    Twitch search can list stale duplicates of a category under the same name (e.g. two
    "Anime Shop Simulator ✨"). Get Games returns only the category Twitch maps that name
    to, so defer to it. Candidates with different names stay ambiguous: that is a real
    choice (e.g. two different Rainbow Six 3 expansions), not a duplicate.
    """
    names = {g.name for g in tied}
    if len(names) != 1:
        return []
    ids = {g.id for g in tied}
    canonical = [g for g in lookup.get_games_by_name(list(names)) if g.id in ids]
    return canonical if len(canonical) == 1 else []


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
