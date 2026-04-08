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

APIFY_POP_THRESHOLD = 50_000


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
    has_apify: bool = False,
    store: LeadStore | None = None,
) -> list[ScrapeTarget]:
    """Resolve a query + location into concrete scrape targets."""
    store = store or LeadStore()
    ensure_cities_seeded(store)

    if source == LeadSource.LINKEDIN:
        return _linkedin_tiles(query, location, max_results_per_tile, store)
    else:
        return _gmaps_tiles_resolve(query, location, max_results_per_tile, has_apify, source, store)


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
    has_apify: bool,
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
        cc = city.get("country_code", "US")
        state = city.get("state", "")
        pop = city["population"]
        use_apify = has_apify and pop >= APIFY_POP_THRESHOLD
        loc = f"{city['name']}, {state}" if state else city["name"]

        targets.append(ScrapeTarget(
            source=source,
            query=query,
            location=loc,
            max_results=max_results,
            extra={
                "tile_city": city["name"],
                "tile_state": state,
                "tile_country": cc,
                "tile_pop": pop,
                "tile_lat": city["lat"],
                "tile_lng": city["lng"],
                "tile_zoom": 13,
                "tile_scraper": "apify" if use_apify else "playwright",
            },
        ))

    apify_count = sum(1 for t in targets if t.extra.get("tile_scraper") == "apify")
    logger.info(
        "Google Maps: '%s' → %d tiles (%d apify, %d playwright)",
        location, len(targets), apify_count, len(targets) - apify_count,
    )
    return targets


def _resolve_cities_for_gmaps(location_lower: str, store: LeadStore) -> list[dict] | None:
    """Resolve a location to city list for Google Maps tiling."""
    country_codes = _get_country_codes(store)

    # Parse "location, qualifier".
    parts = [p.strip() for p in location_lower.split(",")]
    location_part = parts[0]
    qualifier = parts[1] if len(parts) > 1 else None

    scope_country: str | None = None
    scope_state: str | None = None

    if qualifier:
        cc = country_codes.get(qualifier)
        if cc:
            scope_country = cc
        sc = STATE_CODES.get(qualifier)
        if sc:
            scope_country = "US"
            scope_state = sc

    # US state?
    state_code = STATE_CODES.get(location_part)
    if state_code and not qualifier:
        return store.get_cities(state_code, country_code="US") or None

    # Country?
    country_code = country_codes.get(location_part)
    if country_code and not qualifier:
        return store.get_cities_by_country(country_code) or None

    # Region (with optional country scope)?
    target = _normalize_accent(location_part)
    search_countries = [{"country_code": scope_country}] if scope_country else store.get_all_countries()

    for country in search_countries:
        cc = country["country_code"]
        if scope_state:
            cities = store.get_cities(scope_state, country_code="US")
            matched = [c for c in cities if _normalize_accent(c["name"]) == target]
            if matched:
                return matched
            continue
        for region in store.get_regions(cc):
            if _normalize_accent(region) == target:
                cities = store.get_cities(region, country_code=cc)
                if cities:
                    return cities

    # Scoped city lookup.
    if scope_country:
        cities = store.get_cities_by_country(scope_country)
        matched = [c for c in cities if _normalize_accent(c["name"]) == target]
        if matched:
            return matched

    return None
