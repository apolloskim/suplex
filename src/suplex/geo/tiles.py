"""Tile planner — resolves geographic scope into concrete scrape targets.

Google Maps: tiles by city coordinates (viewport-based search)
LinkedIn: tiles by unique autocomplete strings (geo entity-based search)

Both support:
  "Austin, TX"  → 1 target (city passthrough)
  "Texas"       → all cities in TX
  "US"          → all cities in US
  "Japan"       → all cities in JP
  "Tokyo"       → all cities in Tokyo prefecture
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

    location_lower = location.strip().lower()

    # Resolve the geographic scope: state, country, region, or city passthrough.
    cities = _resolve_cities(location_lower, store)

    if cities is None:
        # City-level passthrough — unrecognized or specific city.
        return [ScrapeTarget(
            source=source, query=query, location=location, max_results=max_results_per_tile,
        )]

    # Branch by source.
    if source == LeadSource.LINKEDIN:
        return _linkedin_tiles(cities, query, max_results_per_tile, store)
    else:
        return _gmaps_tiles(cities, query, max_results_per_tile, has_apify, source)


# ---------------------------------------------------------------------------
# Geographic resolution (shared)
# ---------------------------------------------------------------------------


def _resolve_cities(location_lower: str, store: LeadStore) -> list[dict] | None:
    """Resolve a location string to a list of city dicts, or None for passthrough."""

    # US state?
    state_code = STATE_CODES.get(location_lower)
    if state_code:
        cities = store.get_cities(state_code, country_code="US")
        return cities if cities else None

    # Country?
    country_codes = _get_country_codes(store)
    country_code = country_codes.get(location_lower)
    if country_code:
        cities = store.get_cities_by_country(country_code)
        return cities if cities else None

    # Foreign region? (province, prefecture, etc.)
    target = _normalize_accent(location_lower)
    for country in store.get_all_countries():
        cc = country["country_code"]
        for region in store.get_regions(cc):
            if _normalize_accent(region) == target:
                cities = store.get_cities(region, country_code=cc)
                if cities:
                    return cities

    return None


# ---------------------------------------------------------------------------
# Google Maps tiles (coordinate-based)
# ---------------------------------------------------------------------------


def _gmaps_tiles(
    cities: list[dict],
    query: str,
    max_results: int,
    has_apify: bool,
    source: LeadSource,
) -> list[ScrapeTarget]:
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
        "Google Maps: %d tiles (%s) — %d apify, %d playwright",
        len(targets), query, apify_count, len(targets) - apify_count,
    )
    return targets


# ---------------------------------------------------------------------------
# LinkedIn tiles (autocomplete string-based)
# ---------------------------------------------------------------------------


def _linkedin_tiles(
    cities: list[dict],
    query: str,
    max_results: int,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Generate one target per unique LinkedIn autocomplete string.

    For each city, look up ALL autocomplete suggestions. Each unique suggestion
    becomes its own Apify run. Deduplicated so "Austin, Texas Metropolitan Area"
    only appears once even if multiple cities map to it.

    Suggestions are filtered to only include those relevant to the target geography.
    """
    # Determine the target geography for filtering suggestions.
    # Collect unique state full names (e.g., "texas") and country names.
    target_country_codes = set()
    target_state_codes = set()
    for city in cities:
        cc = city.get("country_code", "")
        if cc:
            target_country_codes.add(cc)
        state = city.get("state", "")
        if state:
            target_state_codes.add(state)

    # For US state searches, we need state full names for matching.
    # "TX" → "Texas" for matching against "Houston, Texas, United States"
    _CODE_TO_STATE_NAME = {v: k.title() for k, v in STATE_CODES.items() if len(k) > 2}
    target_state_names = set()
    for sc in target_state_codes:
        name = _CODE_TO_STATE_NAME.get(sc, sc)
        target_state_names.add(name.lower())

    # For non-US, get country names.
    target_country_names = set()
    for c in store.get_all_countries():
        if c["country_code"] in target_country_codes:
            target_country_names.add(c["country_name"].lower())

    is_single_us_state = len(target_country_codes) == 1 and "US" in target_country_codes and len(target_state_names) == 1
    _target_state_code = next(iter(target_state_codes)) if target_state_codes else ""

    def _suggestion_matches_geography(suggestion: str, city_name: str) -> bool:
        """Check if an autocomplete suggestion is relevant to our target area."""
        s_lower = suggestion.lower()

        if is_single_us_state:
            # Strict: must contain the state name (e.g., "texas").
            state_name = next(iter(target_state_names))
            if state_name in s_lower:
                return True
            # Allow metro patterns only if they also reference the target state.
            cn = city_name.lower()
            is_metro = "area" in s_lower or "metropolitan" in s_lower
            if is_metro and (s_lower.startswith(cn) or s_lower.startswith(f"greater {cn}")):
                # Verify the metro string also references our state.
                if state_name in s_lower or f", {_target_state_code.lower()}" in s_lower:
                    return True
            # Allow "{city}, {state_code}" format (e.g., "South Houston, TX").
            if f", {_target_state_code.lower()}" in s_lower or s_lower.endswith(f" {_target_state_code.lower()}"):
                return True
            return False

        # Country-level: must contain the country name.
        if target_country_names and any(cn in s_lower for cn in target_country_names):
            return True
        # Metro patterns: "Greater {city}", "{city} Area" — only if it looks like a metro.
        cn = city_name.lower()
        is_metro = "area" in s_lower or "metropolitan" in s_lower
        if is_metro and (s_lower.startswith(cn) or s_lower.startswith(f"greater {cn}")):
            return True
        return False

    seen_autocompletes: dict[str, dict] = {}

    for city in cities:
        city_name = city["name"]
        suggestions = store.get_linkedin_location(city_name)

        if suggestions:
            for suggestion in suggestions:
                if suggestion not in seen_autocompletes and _suggestion_matches_geography(suggestion, city_name):
                    seen_autocompletes[suggestion] = city
        else:
            state = city.get("state", "")
            cc = city.get("country_code", "US")
            if state:
                fallback = f"{city_name}, {state}"
            else:
                fallback = city_name

            if fallback not in seen_autocompletes:
                seen_autocompletes[fallback] = city

    # Create one target per unique autocomplete string.
    targets = []
    for autocomplete_str, origin_city in seen_autocompletes.items():
        targets.append(ScrapeTarget(
            source=LeadSource.LINKEDIN,
            query=query,
            location=autocomplete_str,  # Already in LinkedIn's canonical format.
            max_results=max_results,
            extra={
                "tile_autocomplete": autocomplete_str,
                "tile_origin_city": origin_city["name"],
                "tile_state": origin_city.get("state", ""),
                "tile_country": origin_city.get("country_code", "US"),
                "tile_pop": origin_city["population"],
                "li_resolved": True,  # Tell the scraper not to re-resolve.
            },
        ))

    logger.info(
        "LinkedIn: %d cities → %d unique autocomplete tiles (%s)",
        len(cities), len(targets), query,
    )
    return targets
