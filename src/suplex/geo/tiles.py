"""Tile planner — resolves geographic scope into concrete scrape targets.

Google Maps: tiles by city coordinates (viewport-based search)
LinkedIn: tiles by unique autocomplete strings (looked up from geo_map table)

Both support any granularity:
  "Austin, TX"                → 1-2 autocomplete tiles
  "Texas"                     → all TX autocomplete strings
  "US"                        → all US autocomplete strings
  "Japan"                     → all JP autocomplete strings
  "London, UK"                → UK London only
  "London, Kentucky"          → KY London only
  "North Holland, Netherlands" → North Holland only
"""

from __future__ import annotations

import logging
import re
import unicodedata

from suplex.geo.seed import ensure_cities_seeded
from suplex.models import LeadSource, ScrapeTarget
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

# US state name → code mapping.
STATE_CODES: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
# Code → lowercase full name ("TX" → "texas"). Built before the codes are added as keys.
STATE_NAMES: dict[str, str] = {code: name for name, code in STATE_CODES.items()}
STATE_CODES.update({v.lower(): v for v in STATE_CODES.values()})

# Common country aliases.
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US", "united states of america": "US", "nationwide": "US", "national": "US",
    "uk": "GB", "england": "GB", "britain": "GB", "great britain": "GB",
    "korea": "KR", "south korea": "KR",
    "uae": "AE", "czech republic": "CZ", "czechia": "CZ",
    "burma": "MM", "myanmar": "MM", "ivory coast": "CI",
    "congo": "CD", "palestine": "PS",
}

_country_cache: dict[str, str] | None = None


def _get_country_codes(store: LeadStore) -> dict[str, str]:
    global _country_cache
    if _country_cache is not None:
        return _country_cache
    mapping: dict[str, str] = {}
    for c in store.get_all_countries():
        cc = c["country_code"]
        mapping[c["country_name"].lower()] = cc
        mapping[cc.lower()] = cc
    mapping.update(_COUNTRY_ALIASES)
    _country_cache = mapping
    return mapping


def _normalize_accent(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def resolve_tiles(
    query: str,
    location: str,
    *,
    max_results_per_tile: int = 0,
    source: LeadSource = LeadSource.GOOGLE_MAPS,
    store: LeadStore | None = None,
) -> list[ScrapeTarget]:
    """Resolve a query + location into concrete scrape targets."""
    store = store or LeadStore()
    ensure_cities_seeded(store)

    if source == LeadSource.LINKEDIN:
        return _linkedin_tiles(query, location, max_results_per_tile, store)
    return _gmaps_tiles_resolve(query, location, max_results_per_tile, source, store)


def filter_tiles(
    tiles: list[ScrapeTarget],
    *,
    only: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[ScrapeTarget]:
    """Apply the ``--only`` / ``--exclude`` region filters to a list of tiles."""
    if only:
        tiles = [t for t in tiles if tile_matches(t, only)]
    if exclude:
        tiles = [t for t in tiles if not tile_matches(t, exclude)]
    return tiles


def tile_matches(tile: ScrapeTarget, filters: list[str]) -> bool:
    """Does a tile fall inside any of the given regions?

    A filter can be a US state code or name (TX, Texas), a country code (JP),
    or a region / city name (Tokyo, "North Holland"). Matching is accent- and
    case-insensitive and always on whole words: "IN" must select Indiana, not
    every location that happens to contain the letters "in".
    """
    state = _normalize_accent(tile.extra.get("tile_state", ""))
    city = _normalize_accent(tile.extra.get("tile_city", ""))
    country = tile.extra.get("tile_country", "").upper()
    location = _normalize_accent(tile.location or "")
    location_parts = [part.strip() for part in location.split(",")]

    for raw in filters:
        wanted = _normalize_accent(raw.strip())
        if not wanted:
            continue

        if country and wanted.upper() == country:
            return True

        state_code = STATE_CODES.get(wanted)
        if state_code and _in_us_state(state_code, state, country, location_parts):
            return True
        if state_code and (len(wanted) == 2 or (state and country in ("US", ""))):
            # For a US tile, a state filter is decided by the state alone. Falling
            # through to free-text matching would let "OR" select Oregon, Illinois
            # and "IN" select anything containing the letters "in".
            continue

        if state and (_has_phrase(state, wanted) or _has_phrase(wanted, state)):
            return True
        if city and _has_phrase(city, wanted):
            return True
        if _has_phrase(location, wanted):
            return True

    return False


def _in_us_state(state_code: str, tile_state: str, tile_country: str, location_parts: list[str]) -> bool:
    """Is a tile inside the given US state?

    Google Maps tiles carry the state code. LinkedIn tiles only have text such
    as "Austin, Texas, United States", "Texas, United States" or "Austin, Texas
    Metropolitan Area", so the state is looked for by name — skipping the
    leading city part, because "Oregon, Illinois, United States" is not Oregon.
    """
    code, name = state_code.lower(), STATE_NAMES[state_code]
    if tile_state:
        return tile_state == code and tile_country in ("US", "")
    if code in location_parts:
        return True
    region_parts = location_parts[1:] if len(location_parts) >= 3 else location_parts
    return any(_has_phrase(part, name) for part in region_parts)


def _has_phrase(text: str, phrase: str) -> bool:
    """True if ``phrase`` occurs in ``text`` on word boundaries."""
    if not text or not phrase:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


# ---------------------------------------------------------------------------
# LinkedIn tiles — lookup from geo_map
# ---------------------------------------------------------------------------


def _linkedin_tiles(
    query: str,
    location: str,
    max_results: int,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Generate LinkedIn tiles by looking up the location in the geo_map.

    The geo_map was built from autocomplete data — every geographic entity
    (country, region, city, metro) is a lookup key that maps to the exact
    autocomplete strings LinkedIn accepts.
    """
    location_lower = location.strip().lower()

    # Look up in geo map — try both accented and stripped forms, take the larger set.
    autocompletes = store.get_linkedin_geo(location_lower)
    normalized = _normalize_accent(location_lower)
    if normalized != location_lower:
        alt = store.get_linkedin_geo(normalized)
        if len(alt) > len(autocompletes):
            autocompletes = alt

    if not autocompletes:
        # Passthrough — the location as-is. The actor will attempt its own resolution.
        logger.debug("No geo map entry for '%s' — passing through", location)
        return [ScrapeTarget(
            source=LeadSource.LINKEDIN,
            query=query,
            location=location,
            max_results=max_results,
        )]

    # Deduplicate autocomplete strings.
    unique = list(dict.fromkeys(autocompletes))

    targets = [
        ScrapeTarget(
            source=LeadSource.LINKEDIN,
            query=query,
            location=ac,
            max_results=max_results,
            extra={"li_resolved": True, "tile_autocomplete": ac},
        )
        for ac in unique
    ]

    logger.info("LinkedIn: '%s' → %d autocomplete tiles", location, len(targets))
    return targets


# ---------------------------------------------------------------------------
# Google Maps tiles — city coordinates
# ---------------------------------------------------------------------------


def _gmaps_tiles_resolve(
    query: str,
    location: str,
    max_results: int,
    source: LeadSource,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Resolve Google Maps tiles via city database."""
    location_lower = location.strip().lower()
    cities = _resolve_cities_for_gmaps(location_lower, store)

    if cities is None:
        return [ScrapeTarget(
            source=source, query=query, location=location, max_results=max_results,
        )]

    targets = []
    for city in cities:
        state = city.get("state", "")
        targets.append(ScrapeTarget(
            source=source,
            query=query,
            location=f"{city['name']}, {state}" if state else city["name"],
            max_results=max_results,
            extra={
                "tile_city": city["name"],
                "tile_state": state,
                "tile_country": city.get("country_code", "US"),
                "tile_pop": city["population"],
                "tile_lat": city["lat"],
                "tile_lng": city["lng"],
                "tile_zoom": 13,
            },
        ))

    logger.info("Google Maps: '%s' → %d tiles", location, len(targets))
    return targets


def _resolve_cities_for_gmaps(location_lower: str, store: LeadStore) -> list[dict] | None:
    """Resolve a location to city list for Google Maps tiling."""
    country_codes = _get_country_codes(store)

    # Parse "location, qualifier".
    parts = [p.strip() for p in location_lower.split(",")]
    location_part = parts[0]
    qualifier = parts[1] if len(parts) > 1 else None

    target = _normalize_accent(location_part)

    # "<city>, <qualifier>": the qualifier is a US state, a country, or both
    # ("CA" is California and Canada, "Georgia" is a state and a country).
    # Try the state first, then the country. Never widen beyond the qualifier:
    # "Springfield, TX" must not turn into every Springfield in the US.
    if qualifier:
        scope_state = STATE_CODES.get(qualifier)
        scope_country = country_codes.get(qualifier)
        if scope_state:
            in_state = store.get_cities(scope_state, country_code="US")
            matched = [c for c in in_state if _normalize_accent(c["name"]) == target]
            if matched:
                return matched
        if scope_country:
            for region in store.get_regions(scope_country):
                if _normalize_accent(region) == target:
                    cities = store.get_cities(region, country_code=scope_country)
                    if cities:
                        return cities
            in_country = store.get_cities_by_country(scope_country)
            matched = [c for c in in_country if _normalize_accent(c["name"]) == target]
            if matched:
                return matched
        if scope_state or scope_country:
            return None

    # US state?
    state_code = STATE_CODES.get(location_part)
    if state_code and not qualifier:
        return store.get_cities(state_code, country_code="US") or None

    # Country?
    country_code = country_codes.get(location_part)
    if country_code and not qualifier:
        return store.get_cities_by_country(country_code) or None

    # A region name in any country ("Bavaria", "North Holland").
    for country in store.get_all_countries():
        cc = country["country_code"]
        for region in store.get_regions(cc):
            if _normalize_accent(region) == target:
                cities = store.get_cities(region, country_code=cc)
                if cities:
                    return cities

    return None
