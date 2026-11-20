"""Tile planning and --only/--exclude filtering, against a small hand-seeded city table."""

from __future__ import annotations

import pytest

from suplex.geo import tiles as tiles_module
from suplex.geo.tiles import filter_tiles, resolve_tiles, tile_matches
from suplex.models import LeadSource, ScrapeTarget

CITIES = [
    # name, state, country_code, country_name
    ("Austin", "TX", "US", "United States"),
    ("Houston", "TX", "US", "United States"),
    ("Portland", "OR", "US", "United States"),
    ("Oregon", "IL", "US", "United States"),
    ("New York City", "NY", "US", "United States"),
    ("Fort Wayne", "IN", "US", "United States"),
    ("Indiana", "PA", "US", "United States"),
    ("Springfield", "IL", "US", "United States"),
    ("Toronto", "OH", "US", "United States"),
    ("Toronto", "Ontario", "CA", "Canada"),
    ("Tokyo", "Tōkyō", "JP", "Japan"),
    ("Tbilisi", "Tbilisi", "GE", "Georgia"),
    ("Amsterdam", "Noord-Holland", "NL", "Netherlands"),
]


@pytest.fixture
def geo_store(store, monkeypatch):
    """A store with a handful of cities; skips the multi-second bundled-data seed."""
    store.seed_cities([
        {"name": n, "state": s, "country_code": cc, "country_name": cn, "lat": 1.0, "lng": 2.0, "pop": 1000 - i}
        for i, (n, s, cc, cn) in enumerate(CITIES)
    ])
    monkeypatch.setattr(tiles_module, "ensure_cities_seeded", lambda store=None: None)
    monkeypatch.setattr(tiles_module, "_country_cache", None)
    return store


def _locations(tiles: list[ScrapeTarget]) -> list[str]:
    return sorted(t.location for t in tiles)


def _linkedin(location: str) -> ScrapeTarget:
    return ScrapeTarget(source=LeadSource.LINKEDIN, query="dentist", location=location)


# --- filtering -------------------------------------------------------------


def test_state_code_filters_do_not_match_inside_other_words(geo_store) -> None:
    us = resolve_tiles("dentist", "US", store=geo_store)

    # "in" is a substring of "Austin"; "or" of "New York" and "Fort Wayne".
    assert _locations(filter_tiles(us, only=["IN"])) == ["Fort Wayne, IN"]
    assert _locations(filter_tiles(us, only=["OR"])) == ["Portland, OR"]


def test_state_filters_ignore_cities_named_after_states(geo_store) -> None:
    us = resolve_tiles("dentist", "US", store=geo_store)

    # Oregon, IL and Indiana, PA are not in Oregon or Indiana.
    assert _locations(filter_tiles(us, only=["Oregon"])) == ["Portland, OR"]
    assert "Indiana, PA" in _locations(filter_tiles(us, exclude=["IN"]))


def test_exclude_removes_whole_states(geo_store) -> None:
    us = resolve_tiles("dentist", "US", store=geo_store)

    remaining = _locations(filter_tiles(us, exclude=["TX", "IL"]))

    assert "Austin, TX" not in remaining and "Springfield, IL" not in remaining
    assert "Portland, OR" in remaining


def test_region_and_country_filters_are_accent_insensitive(geo_store) -> None:
    jp = resolve_tiles("dentist", "Japan", store=geo_store)
    nl = resolve_tiles("dentist", "Netherlands", store=geo_store)

    assert len(filter_tiles(jp, only=["tokyo"])) == 1
    assert len(filter_tiles(jp, only=["JP"])) == 1
    assert len(filter_tiles(nl, only=["Holland"])) == 1
    assert filter_tiles(jp, only=["Osaka"]) == []


@pytest.mark.parametrize(
    ("location", "code", "expected"),
    [
        ("Burlington, Vermont, United States", "VT", True),   # code never appears in the text
        ("Austin, Texas Metropolitan Area", "TX", True),
        ("Texas, United States", "TX", True),
        ("Portland, OR", "OR", True),
        ("Oregon, Illinois, United States", "OR", False),      # a city called Oregon
        ("New York, New York, United States", "OR", False),    # "or" inside "York"
        ("La Porte, Texas, United States", "LA", False),       # "La" as a word in a city name
    ],
)
def test_linkedin_tiles_match_states_by_name(location: str, code: str, expected: bool) -> None:
    assert tile_matches(_linkedin(location), [code]) is expected


# --- resolution ------------------------------------------------------------


def test_city_with_state_qualifier_resolves_to_that_city(geo_store) -> None:
    tiles = resolve_tiles("dentist", "Austin, TX", store=geo_store)

    assert [(t.location, t.extra["tile_state"]) for t in tiles] == [("Austin, TX", "TX")]


def test_unknown_city_in_a_state_does_not_widen_to_the_whole_country(geo_store) -> None:
    # There is a Springfield in IL but none in TX. The old lookup fell back to
    # every Springfield in the US and scraped the wrong state.
    tiles = resolve_tiles("dentist", "Springfield, TX", store=geo_store)

    assert len(tiles) == 1
    assert tiles[0].location == "Springfield, TX"
    assert "tile_state" not in tiles[0].extra  # passed through for Google to interpret


def test_ambiguous_qualifier_tries_state_then_country(geo_store) -> None:
    # "CA" is California and Canada; "Georgia" is a state and a country.
    toronto = resolve_tiles("dentist", "Toronto, CA", store=geo_store)
    tbilisi = resolve_tiles("dentist", "Tbilisi, Georgia", store=geo_store)

    assert [t.extra["tile_country"] for t in toronto] == ["CA"]
    assert [t.extra["tile_country"] for t in tbilisi] == ["GE"]


def test_state_expands_to_all_its_cities(geo_store) -> None:
    assert _locations(resolve_tiles("dentist", "Texas", store=geo_store)) == ["Austin, TX", "Houston, TX"]
