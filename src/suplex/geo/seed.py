"""Seed the cities and LinkedIn location tables from bundled data."""

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

MIN_POPULATION = 0


def ensure_cities_seeded(store: LeadStore | None = None) -> None:
    """Seed cities + LinkedIn locations if needed. Called lazily on first geo query."""
    store = store or LeadStore()

    # --- Cities ---
    if not store.cities_seeded():
        _seed_cities(store)

    # --- LinkedIn locations (separate check, can update independently) ---
    _ensure_linkedin_seeded(store)


def _seed_cities(store: LeadStore) -> None:
    count = 0

    if US_CITIES_PATH.exists():
        logger.info("Seeding US cities...")
        with open(US_CITIES_PATH) as f:
            us_cities = json.load(f)
        rows = [
            {
                "name": c["name"], "state": c["state"],
                "country_code": "US", "country_name": "United States",
                "lat": c["lat"], "lng": c["lng"], "pop": c["pop"],
            }
            for c in us_cities if c["pop"] >= MIN_POPULATION
        ]
        count += store.seed_cities(rows)

    if WORLD_CITIES_PATH.exists():
        logger.info("Seeding world cities...")
        with open(WORLD_CITIES_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = []
            for row in reader:
                if row["iso2"] == "US":
                    continue
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


def _ensure_linkedin_seeded(store: LeadStore) -> None:
    if not LINKEDIN_LOCATIONS_PATH.exists():
        return

    # Check if we need to seed or update.
    with open(LINKEDIN_LOCATIONS_PATH) as f:
        bundled = json.load(f)
    bundled_count = len(bundled)

    with store._conn() as conn:
        db_count = conn.execute("SELECT COUNT(*) FROM linkedin_locations").fetchone()[0]

    needs_seed = db_count == 0 or bundled_count > db_count

    if not needs_seed:
        # Also check geo map.
        with store._conn() as conn:
            geo_count = conn.execute("SELECT COUNT(*) FROM linkedin_geo_map").fetchone()[0]
        if geo_count == 0:
            needs_seed = True  # Geo map missing, rebuild from existing data.
            _rebuild_geo_map(store)
        return

    if db_count > 0:
        logger.info("LinkedIn locations outdated (%d → %d) — updating", db_count, bundled_count)

    # Seed locations.
    logger.info("Seeding LinkedIn locations...")
    rows = []
    for query_key, suggestions in bundled.items():
        if isinstance(suggestions, list) and suggestions:
            rows.append({
                "query": query_key,
                "suggestions": json.dumps(suggestions),
                "scraped_at": "",
            })
    store.seed_linkedin_locations(rows)
    logger.info("Seeded %d LinkedIn locations", len(rows))

    # Rebuild geo map.
    _rebuild_geo_map(store)


def _rebuild_geo_map(store: LeadStore) -> None:
    from suplex.geo.build_geo_map import build_geo_map
    logger.info("Rebuilding geo map...")
    with store._conn() as conn:
        conn.execute("DELETE FROM linkedin_geo_map")
    build_geo_map(store)
