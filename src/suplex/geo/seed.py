"""Seed the cities table from bundled data."""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

GEO_DIR = Path(__file__).parent
US_CITIES_PATH = GEO_DIR / "us-cities.json"
WORLD_CITIES_PATH = GEO_DIR / "worldcities.csv"
LINKEDIN_LOCATIONS_PATH = GEO_DIR / "linkedin-location-autocomplete.json"

MIN_POPULATION = 0  # Include all cities.


def ensure_cities_seeded(store: LeadStore | None = None) -> None:
    """Seed cities if not already done. Called lazily on first geo query."""
    store = store or LeadStore()
    if store.cities_seeded():
        return

    count = 0

    # Seed US cities (higher quality data with state codes).
    if US_CITIES_PATH.exists():
        logger.info("Seeding US cities...")
        with open(US_CITIES_PATH) as f:
            us_cities = json.load(f)
        rows = [
            {
                "name": c["name"],
                "state": c["state"],
                "country_code": "US",
                "country_name": "United States",
                "lat": c["lat"],
                "lng": c["lng"],
                "pop": c["pop"],
            }
            for c in us_cities
            if c["pop"] >= MIN_POPULATION
        ]
        count += store.seed_cities(rows)

    # Seed world cities (excluding US since we already have better data).
    if WORLD_CITIES_PATH.exists():
        logger.info("Seeding world cities...")
        with open(WORLD_CITIES_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                if row["iso2"] == "US":
                    continue  # Already seeded with better US data.
                pop_str = row.get("population", "")
                pop = int(float(pop_str)) if pop_str else 0
                if pop < MIN_POPULATION:
                    continue
                rows.append({
                    "name": row["city_ascii"],
                    "state": row.get("admin_name", "") or "",
                    "country_code": row["iso2"],
                    "country_name": row["country"],
                    "lat": float(row["lat"]),
                    "lng": float(row["lng"]),
                    "pop": pop,
                })
        count += store.seed_cities(rows)

    logger.info("Seeded %d cities total", count)

    # Seed LinkedIn locations if available and not yet done.
    if not store.linkedin_locations_seeded() and LINKEDIN_LOCATIONS_PATH.exists():
        logger.info("Seeding LinkedIn locations...")
        with open(LINKEDIN_LOCATIONS_PATH) as f:
            li_data = json.load(f)

        rows = []
        # Compact format: {"city": ["suggestion1", "suggestion2", ...]}
        for query_key, suggestions in li_data.items():
            if isinstance(suggestions, list) and suggestions:
                rows.append({
                    "query": query_key,
                    "suggestions": json.dumps(suggestions),
                    "scraped_at": "",
                })

        li_count = store.seed_linkedin_locations(rows)
        logger.info("Seeded %d LinkedIn locations", li_count)
