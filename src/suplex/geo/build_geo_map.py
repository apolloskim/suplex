"""Build the linkedin_geo_map table from autocomplete data.

Parses every autocomplete string and creates lookup keys at every level:
  "Amsterdam, North Holland, Netherlands"
  → keys: "amsterdam", "north holland", "netherlands",
           "amsterdam, north holland", "amsterdam, netherlands",
           "north holland, netherlands"

Also handles:
  "Seoul Incheon Metropolitan Area" → "seoul", "incheon", "seoul incheon"
  "Greater London, England, United Kingdom" → "london", "england", "united kingdom", "uk"
  "Austin, Texas Metropolitan Area" → "austin", "texas", "austin, texas"

Run this whenever new autocomplete data is added.
"""

from __future__ import annotations

import json
import logging
import re

from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

# Common country aliases to add as lookup keys.
COUNTRY_ALIASES = {
    "united kingdom": ["uk", "britain", "england"],
    "united states": ["us", "usa"],
    "south korea": ["korea"],
    "united arab emirates": ["uae"],
    "czech republic": ["czechia"],
}


def build_geo_map(store: LeadStore) -> int:
    """Build the geo map from existing linkedin_locations data in SQLite."""
    pairs: set[tuple[str, str]] = set()

    with store._conn() as conn:
        rows = conn.execute("SELECT query, suggestions FROM linkedin_locations").fetchall()

    for row in rows:
        suggestions = json.loads(row["suggestions"])
        for autocomplete in suggestions:
            keys = _extract_keys(autocomplete)
            for key in keys:
                pairs.add((key, autocomplete))

    rows_list = list(pairs)
    count = store.seed_linkedin_geo_map(rows_list)
    logger.info("Built geo map: %d entries from %d autocomplete strings", count, len(rows))
    return count


def _extract_keys(autocomplete: str) -> list[str]:
    """Extract all lookup keys from an autocomplete string."""
    keys: list[str] = []

    # Clean up the string.
    clean = autocomplete.strip()
    if not clean:
        return keys

    # Split by comma to get geographic parts.
    parts = [p.strip() for p in clean.split(",")]
    parts = [p for p in parts if p]

    # Each part is a lookup key.
    for part in parts:
        key = part.lower().strip()
        if key and len(key) > 1:
            keys.append(key)

    # Combinations: "city, state", "city, country", "state, country"
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            combo = f"{parts[i].lower()}, {parts[j].lower()}"
            keys.append(combo)

    # Handle "X Metropolitan Area", "Greater X", "X Area" patterns.
    area_match = re.match(
        r"^(?:greater\s+)?(.+?)(?:\s+(?:metropolitan\s+)?area)$",
        clean,
        re.IGNORECASE,
    )
    if area_match:
        inner = area_match.group(1).strip()
        # "Seoul Incheon" → "seoul", "incheon", "seoul incheon"
        inner_parts = inner.split()
        for ip in inner_parts:
            key = ip.lower().strip()
            if key and len(key) > 1:
                keys.append(key)
        if len(inner_parts) > 1:
            keys.append(inner.lower())

    # Add the full string itself as a key.
    keys.append(clean.lower())

    # Add country aliases.
    for country, aliases in COUNTRY_ALIASES.items():
        if country in clean.lower():
            keys.extend(aliases)

    # Add accent-stripped variants.
    import unicodedata
    stripped: list[str] = []
    for key in keys:
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", key) if unicodedata.category(c) != "Mn"
        )
        if normalized != key:
            stripped.append(normalized)
    keys.extend(stripped)

    # Deduplicate.
    return list(dict.fromkeys(keys))
