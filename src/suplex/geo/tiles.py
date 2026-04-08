"""Tile planner — resolves geographic scope into concrete scrape targets.

"dental hygienists in Texas"    → 409 targets (every TX city pop >= 5k)
"dental hygienists in US"       → 7,497 targets (every US city)
"dentists in Japan"             → 1,344 targets (every JP city pop >= 5k)
"dentists in Austin, TX"        → 1 target (passthrough)
"""

from __future__ import annotations

import logging

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

# Common aliases that don't match the DB's country_name exactly.
_COUNTRY_ALIASES: dict[str, str] = {
    "usa": "US", "united states of america": "US", "nationwide": "US", "national": "US",
    "uk": "GB", "england": "GB", "britain": "GB", "great britain": "GB",
    "korea": "KR", "south korea": "KR",
    "uae": "AE",
    "czech republic": "CZ", "czechia": "CZ",
    "burma": "MM", "myanmar": "MM",
    "ivory coast": "CI",
    "congo": "CD",
    "palestine": "PS",
}

# Built dynamically from the DB on first call.
_country_cache: dict[str, str] | None = None


def _get_country_codes(store: LeadStore) -> dict[str, str]:
    """Build country name → ISO2 mapping from the database + aliases."""
    global _country_cache
    if _country_cache is not None:
        return _country_cache

    mapping: dict[str, str] = {}
    for c in store.get_all_countries():
        cc = c["country_code"]
        name = c["country_name"]
        mapping[name.lower()] = cc
        mapping[cc.lower()] = cc  # Accept ISO2 codes directly.

    # Add aliases.
    mapping.update(_COUNTRY_ALIASES)

    _country_cache = mapping
    return mapping

APIFY_POP_THRESHOLD = 50_000


def resolve_tiles(
    query: str,
    location: str,
    *,
    max_results_per_tile: int = 0,
    source: LeadSource = LeadSource.GOOGLE_MAPS,
    has_apify: bool = False,
    store: LeadStore | None = None,
) -> list[ScrapeTarget]:
    """Resolve a query + location into concrete scrape targets.

    Handles:
    - City: "Austin, TX" → 1 target (passthrough)
    - US state: "Texas" → all cities in TX
    - Country: "Japan", "UK", "Brazil" → all cities in that country
    """
    store = store or LeadStore()
    ensure_cities_seeded(store)

    location_lower = location.strip().lower()

    # US state?
    state_code = STATE_CODES.get(location_lower)
    if state_code:
        return _tiles_for_region(query, state_code, "US", max_results_per_tile, has_apify, source, store)

    # Country?
    country_codes = _get_country_codes(store)
    country_code = country_codes.get(location_lower)
    if country_code:
        return _tiles_for_country(query, country_code, max_results_per_tile, has_apify, source, store)

    # Maybe a foreign region? Try matching "admin_name" in the DB.
    # e.g., "Tokyo", "Bavaria", "Ontario"
    tiles = _tiles_for_foreign_region(query, location, max_results_per_tile, has_apify, source, store)
    if tiles:
        return tiles

    # City-level passthrough.
    return [ScrapeTarget(
        source=source,
        query=query,
        location=location,
        max_results=max_results_per_tile,
    )]


def _tiles_for_region(
    query: str,
    state: str,
    country_code: str,
    max_results: int,
    has_apify: bool,
    source: LeadSource,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Generate tiles for all cities in a state/region."""
    cities = store.get_cities(state, country_code=country_code)
    if not cities:
        return [ScrapeTarget(source=source, query=query, location=state, max_results=max_results)]

    targets = _cities_to_targets(cities, query, max_results, has_apify, source)

    apify_count = sum(1 for t in targets if t.extra.get("tile_scraper") == "apify")
    logger.info(
        "Generated %d tiles for %s/%s (%s) — %d apify, %d playwright",
        len(targets), country_code, state, query, apify_count, len(targets) - apify_count,
    )
    return targets


def _tiles_for_country(
    query: str,
    country_code: str,
    max_results: int,
    has_apify: bool,
    source: LeadSource,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Generate tiles for all cities in a country."""
    cities = store.get_cities_by_country(country_code)
    if not cities:
        return [ScrapeTarget(source=source, query=query, location=country_code, max_results=max_results)]

    targets = _cities_to_targets(cities, query, max_results, has_apify, source)

    logger.info("Generated %d tiles for %s (%s)", len(targets), country_code, query)
    return targets


def _tiles_for_foreign_region(
    query: str,
    region_name: str,
    max_results: int,
    has_apify: bool,
    source: LeadSource,
    store: LeadStore,
) -> list[ScrapeTarget]:
    """Try to match a region name (province, prefecture, etc.) across all countries."""
    import unicodedata

    def _normalize(s: str) -> str:
        # Strip accents/diacritics and lowercase.
        return "".join(
            c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn"
        )

    target = _normalize(region_name)
    for country in store.get_all_countries():
        cc = country["country_code"]
        regions = store.get_regions(cc)
        for region in regions:
            if _normalize(region) == target:
                return _tiles_for_region(query, region, cc, max_results, has_apify, source, store)
    return []


def _cities_to_targets(
    cities: list[dict],
    query: str,
    max_results: int,
    has_apify: bool,
    source: LeadSource,
) -> list[ScrapeTarget]:
    """Convert a list of city dicts into ScrapeTargets with lat/lng."""
    targets = []
    for city in cities:
        cc = city.get("country_code", "US")
        state = city.get("state", "")
        pop = city["population"]
        use_apify = has_apify and pop >= APIFY_POP_THRESHOLD

        # Build a human-readable location string.
        if cc == "US":
            loc = f"{city['name']}, {state}"
        elif state:
            loc = f"{city['name']}, {state}"
        else:
            loc = city["name"]

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
    return targets
