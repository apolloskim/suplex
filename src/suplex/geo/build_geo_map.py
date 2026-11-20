from __future__ import annotations

import json
import logging
import re

from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

COUNTRY_ALIASES = {
    "united kingdom": ["uk", "britain", "england"],
    "united states": ["us", "usa"],
    "south korea": ["korea"],
    "united arab emirates": ["uae"],
    "czech republic": ["czechia"],
}


def _strip_accents(s: str) -> str:
    import unicodedata
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def build_geo_map(store: LeadStore) -> int:
    pairs: set[tuple[str, str]] = set()

    with store._conn() as conn:
        rows = conn.execute("SELECT query, suggestions FROM linkedin_locations").fetchall()

    for row in rows:
        city_name = row["query"]
        suggestions = json.loads(row["suggestions"])
        for autocomplete in suggestions:
            keys = _extract_keys(autocomplete)
            for key in keys:
                pairs.add((key, autocomplete))

            pairs.add((city_name.lower(), autocomplete))
            stripped = _strip_accents(city_name.lower())
            if stripped != city_name.lower():
                pairs.add((stripped, autocomplete))

    rows_list = list(pairs)
    count = store.seed_linkedin_geo_map(rows_list)
    logger.info("Built geo map: %d entries from %d autocomplete strings", count, len(rows))
    return count


def _extract_keys(autocomplete: str) -> list[str]:
    keys: list[str] = []

    clean = autocomplete.strip()
    if not clean:
        return keys

    parts = [p.strip() for p in clean.split(",")]
    parts = [p for p in parts if p]

    for part in parts:
        key = part.lower().strip()
        if key and len(key) > 1:
            keys.append(key)

    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            combo = f"{parts[i].lower()}, {parts[j].lower()}"
            keys.append(combo)

    area_match = re.match(
        r"^(?:greater\s+)?(.+?)(?:\s+(?:metropolitan\s+)?area)$",
        clean,
        re.IGNORECASE,
    )
    if area_match:
        inner = area_match.group(1).strip()
        inner_parts = inner.split()
        for ip in inner_parts:
            key = ip.lower().strip()
            if key and len(key) > 1:
                keys.append(key)
        if len(inner_parts) > 1:
            keys.append(inner.lower())

    keys.append(clean.lower())

    for country, aliases in COUNTRY_ALIASES.items():
        if country in clean.lower():
            keys.extend(aliases)

    import unicodedata
    stripped: list[str] = []
    for key in keys:
        normalized = "".join(
            c for c in unicodedata.normalize("NFD", key) if unicodedata.category(c) != "Mn"
        )
        if normalized != key:
            stripped.append(normalized)
    keys.extend(stripped)

    return list(dict.fromkeys(keys))
