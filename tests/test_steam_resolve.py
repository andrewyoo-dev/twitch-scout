"""Tests for conservative Steam -> Twitch name resolution.

The fake lookup models Helix: Get Games is an exact (case-insensitive) name match, and
Search Categories returns whatever fuzzy candidates it is told to. The load-bearing
cases are the safety ones: a series-name category must never be picked for a longer
title, and an ambiguous search must stay unresolved.
"""

from __future__ import annotations

import logging

import pytest

from twitch_scout.steam import resolve
from twitch_scout.steam.client import OwnedSteamGame
from twitch_scout.steam.resolve import load_aliases, parse_aliases, resolve_names
from twitch_scout.twitch.models import HelixGame


class FakeHelix:
    def __init__(self, categories: list[str], search: dict[str, list[str]] | None = None) -> None:
        self._ids = {name: str(i) for i, name in enumerate(categories, start=1)}
        self._search = search or {}
        self.exact_queries: list[list[str]] = []
        self.search_queries: list[str] = []

    def _game(self, name: str) -> HelixGame:
        return HelixGame(id=self._ids.setdefault(name, f"s{len(self._ids)}"), name=name)

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        self.exact_queries.append(names)
        wanted = {n.casefold() for n in names}
        return [self._game(name) for name in self._ids if name.casefold() in wanted]

    def search_categories(self, query: str) -> list[HelixGame]:
        self.search_queries.append(query)
        return [self._game(name) for name in self._search.get(query, [])]


def _owned(*names: str) -> list[OwnedSteamGame]:
    return [OwnedSteamGame(appid=i, name=n, playtime_minutes=60) for i, n in enumerate(names, 1)]


def _resolved(
    games: list[OwnedSteamGame], helix: FakeHelix, aliases: dict[str, str] | None = None
) -> dict[str, str]:
    result = resolve_names(games, helix, aliases or {})
    by_appid = {g.appid: g.name for g in games}
    return {by_appid[a]: game.name for a, game in result.matches.items()}


# --- exact and normalization tiers ---


def test_exact_match_is_case_and_trademark_insensitive() -> None:
    helix = FakeHelix(["Total War: WARHAMMER III"])
    assert _resolved(_owned("Total War™: Warhammer III"), helix) == {
        "Total War™: Warhammer III": "Total War: WARHAMMER III"
    }


def test_separator_variant_resolves_dash_vs_colon() -> None:
    helix = FakeHelix(["Retro Rewind: Video Store Simulator"])
    games = _owned("Retro Rewind - Video Store Simulator")
    result = resolve_names(games, helix, {})
    assert result.matches[1].name == "Retro Rewind: Video Store Simulator"
    assert result.via[1] == "variant"


def test_space_before_colon_is_normalized() -> None:
    helix = FakeHelix(["DragonSword: Awakening"])
    assert _resolved(_owned("DragonSword : Awakening"), helix) == {
        "DragonSword : Awakening": "DragonSword: Awakening"
    }


@pytest.mark.parametrize(
    ("steam", "twitch"),
    [
        ("The Witcher 3: Wild Hunt - Complete Edition", "The Witcher 3: Wild Hunt"),
        ("Northgard: Definitive Edition", "Northgard"),
        ("Fallout 3 - Game of the Year Edition", "Fallout 3"),
        ("Age of Empires® III (2007)", "Age of Empires III"),
        ("Tree of Savior (English Ver.)", "Tree of Savior"),
        ("The Elder Scrolls V: Skyrim Special Edition", "The Elder Scrolls V: Skyrim"),
        ("Warhammer 40,000: Dawn of War - Soulstorm", "Warhammer 40,000: Dawn of War"),
    ],
)
def test_edition_and_expansion_qualifiers_are_stripped(steam: str, twitch: str) -> None:
    result = resolve_names(_owned(steam), FakeHelix([twitch]), {})
    assert result.matches[1].name == twitch
    assert result.via[1] == "edition"


def test_several_expansions_merge_into_one_category() -> None:
    games = _owned(
        "Warhammer 40,000: Dawn of War - Soulstorm",
        "Warhammer 40,000: Dawn of War - Winter Assault",
    )
    result = resolve_names(games, FakeHelix(["Warhammer 40,000: Dawn of War"]), {})
    assert {g.name for g in result.matches.values()} == {"Warhammer 40,000: Dawn of War"}
    assert len(result.matches) == 2


# --- safety: a wrong match is worse than a miss ---


def test_uncoloned_subtitle_is_not_cut_to_a_bare_series_name() -> None:
    # "Endzone" is a different Twitch category; only the full title is acceptable.
    helix = FakeHelix(["Endzone"], search={"Endzone": ["Endzone", "Endzone 2"]})
    assert _resolved(_owned("Endzone - A World Apart"), helix) == {}


def test_full_title_wins_when_both_exist() -> None:
    helix = FakeHelix(["Endzone", "Endzone: A World Apart"])
    assert _resolved(_owned("Endzone - A World Apart"), helix) == {
        "Endzone - A World Apart": "Endzone: A World Apart"
    }


def test_series_name_category_is_never_accepted_for_a_longer_title() -> None:
    helix = FakeHelix(
        ["Divinity"],
        search={
            "Divinity: Original Sin 2": [],
            "Divinity": [
                "Divinity",
                "Divinity: Original Sin",
                "Divinity: Original Sin II - Definitive Edition",
            ],
        },
    )
    assert _resolved(_owned("Divinity: Original Sin 2"), helix) == {}


def test_ambiguous_subtitled_search_stays_unresolved() -> None:
    helix = FakeHelix(
        [],
        search={
            "Tom Clancy's Rainbow Six 3": [
                "Tom Clancy's Rainbow Six 3: Raven Shield",
                "Tom Clancy's Rainbow Six 3: Black Arrow",
            ]
        },
    )
    assert _resolved(_owned("Tom Clancy's Rainbow Six 3: Gold Edition"), helix) == {}


# --- search tier ---


def test_search_accepts_the_name_plus_a_twitch_subtitle() -> None:
    helix = FakeHelix([], search={"Tiny Aquarium": ["Tiny Aquarium: Social Fishkeeping"]})
    result = resolve_names(_owned("Tiny Aquarium"), helix, {})
    assert result.matches[1].name == "Tiny Aquarium: Social Fishkeeping"
    assert result.via[1] == "search"


def test_search_matches_through_accents_and_punctuation() -> None:
    twitch = "KOTÁMON: My Sis Found A Card, So I Earn $1,000,000"
    helix = FakeHelix([], search={"KOTAMON": [twitch, "KOTARO Survivor"]})
    assert _resolved(_owned("KOTAMON: My Sis Found A Card, So I Earn $1,000,000"), helix) == {
        "KOTAMON: My Sis Found A Card, So I Earn $1,000,000": twitch
    }


def test_search_after_edition_strip_accepts_a_different_subtitle() -> None:
    helix = FakeHelix(
        [],
        search={
            "Dying Light 2": ["Dying Light", "Dying Light 2: Stay Human", "Dying Light: The Beast"]
        },
    )
    assert _resolved(_owned("Dying Light 2: Reloaded Edition"), helix) == {
        "Dying Light 2: Reloaded Edition": "Dying Light 2: Stay Human"
    }


def test_single_equal_result_beats_subtitled_ones() -> None:
    helix = FakeHelix(
        [],
        search={
            "Tom Clancy's Ghost Recon Wildlands": [
                "Tom Clancy's Ghost Recon: Wildlands",
                "Tom Clancy's Ghost Recon Wildlands: Fallen Ghosts",
            ]
        },
    )
    assert _resolved(_owned("Tom Clancy's Ghost Recon® Wildlands"), helix) == {
        "Tom Clancy's Ghost Recon® Wildlands": "Tom Clancy's Ghost Recon: Wildlands"
    }


class DuplicateHelix(FakeHelix):
    """Search lists two same-named categories; Get Games maps the name to ``canonical``."""

    def __init__(self, canonical: str | None) -> None:
        super().__init__([])
        self._canonical = canonical

    def search_categories(self, query: str) -> list[HelixGame]:
        self.search_queries.append(query)
        return [
            HelixGame(id="live", name="Anime Shop Simulator ✨"),
            HelixGame(id="stale", name="Anime Shop Simulator ✨"),
        ]

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        self.exact_queries.append(names)
        if self._canonical and "Anime Shop Simulator ✨" in names:
            return [HelixGame(id=self._canonical, name="Anime Shop Simulator ✨")]
        return []


def test_same_named_duplicates_defer_to_twitchs_canonical_category() -> None:
    result = resolve_names(_owned("anime shop simulator"), DuplicateHelix("live"), {})
    assert result.matches[1].id == "live"
    assert result.via[1] == "search"


def test_duplicates_stay_unresolved_without_a_canonical_answer() -> None:
    assert resolve_names(_owned("anime shop simulator"), DuplicateHelix(None), {}).matches == {}


class CaseTwinHelix(FakeHelix):
    """Live-observed: two categories differ only by case, and Get Games (which matches
    case-insensitively) returns the unrelated small one for either spelling."""

    SMALL = HelixGame(id="small", name="DressMaker")
    BIG = HelixGame(id="big", name="Dressmaker")

    def __init__(self, twins_in_search: bool = True) -> None:
        super().__init__([])
        self._twins = twins_in_search

    def get_games_by_name(self, names: list[str]) -> list[HelixGame]:
        self.exact_queries.append(names)
        return [self.SMALL] if any(n.casefold() == "dressmaker" for n in names) else []

    def search_categories(self, query: str) -> list[HelixGame]:
        self.search_queries.append(query)
        return [self.SMALL, self.BIG] if self._twins else [self.SMALL]


def test_case_twin_prefers_the_exact_case_category() -> None:
    result = resolve_names(_owned("Dressmaker"), CaseTwinHelix(), {})
    assert result.matches[1].id == "big"


def test_case_twin_with_no_exact_case_spelling_is_refused() -> None:
    assert resolve_names(_owned("dressmaker"), CaseTwinHelix(), {}).matches == {}


def test_case_only_difference_without_a_twin_is_accepted() -> None:
    # e.g. Steam "ELDEN RING" vs Twitch "Elden Ring": the only category with that spelling.
    result = resolve_names(_owned("dressmaker"), CaseTwinHelix(twins_in_search=False), {})
    assert result.matches[1].id == "small"


def test_exact_case_hit_needs_no_confirmation_search() -> None:
    helix = FakeHelix(["Dressmaker"])
    assert resolve_names(_owned("Dressmaker"), helix, {}).matches[1].name == "Dressmaker"
    assert helix.search_queries == []


def test_case_mismatch_is_refused_when_search_budget_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resolve, "_MAX_SEARCHES", 0)
    assert resolve_names(_owned("Dressmaker"), CaseTwinHelix(), {}).matches == {}


def test_search_calls_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resolve, "_MAX_SEARCHES", 2)
    helix = FakeHelix([])
    resolve_names(_owned("Obscure One", "Obscure Two", "Obscure Three"), helix, {})
    assert len(helix.search_queries) == 2


# --- aliases and ignored builds ---


def test_alias_maps_an_editorial_rename() -> None:
    helix = FakeHelix(["Counter-Strike"])
    result = resolve_names(
        _owned("Counter-Strike 2"), helix, {"counter-strike 2": "Counter-Strike"}
    )
    assert result.matches[1].name == "Counter-Strike"
    assert result.via[1] == "alias"


def test_alias_with_missing_target_is_reported_not_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = resolve_names(
            _owned("Counter-Strike 2"), FakeHelix([]), {"counter-strike 2": "Gone"}
        )
    assert result.matches == {}
    assert "alias target 'Gone' not found" in caplog.text


@pytest.mark.parametrize(
    "name",
    [
        "Fallout 76 Public Test Server",
        "Squad - Public Testing",
        "Tom Clancy's The Division PTS",
        "Battlefield™ 6 Open Beta",
        "Lost Ark Closed Technical Beta ",
        "Chrono Odyssey Playtest",
    ],
)
def test_test_builds_are_ignored_and_never_queried(name: str) -> None:
    helix = FakeHelix(["Fallout 76", "Squad", "Battlefield 6", "Lost Ark", "Chrono Odyssey"])
    result = resolve_names(_owned(name), helix, {})
    assert result.ignored == frozenset({1})
    assert result.matches == {}
    assert helix.exact_queries == [] and helix.search_queries == []


def test_shipped_alias_file_is_valid() -> None:
    aliases = load_aliases()
    assert aliases["counter-strike 2"] == "Counter-Strike"
    assert all(target.strip() for target in aliases.values())


def test_alias_keys_ignore_case_and_trademarks() -> None:
    aliases = parse_aliases('[aliases]\n"DARK SOULS™: Prepare To Die Edition" = "Dark Souls"\n')
    assert aliases == {"dark souls: prepare to die edition": "Dark Souls"}


@pytest.mark.parametrize(
    "raw",
    ['[aliases]\n"X" = ""\n', '[aliases]\n"X" = 3\n', "aliases = 1\n", "[aliases\n"],
)
def test_malformed_alias_file_fails_loud(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_aliases(raw)
