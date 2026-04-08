"""Apify-based scrapers for Google Maps and LinkedIn.

Google Maps: uses customGeolocation (GeoJSON polygon) for US states when available.
Polygon-based search yields 10-30x more results than text-based locationQuery.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from apify_client import ApifyClientAsync

from suplex.models import Lead, LeadSource, ScrapeTarget

logger = logging.getLogger(__name__)

GOOGLE_MAPS_ACTOR = "compass/crawler-google-places"
LINKEDIN_PEOPLE_ACTOR = "curious_coder/linkedin-search-scraper"

# State polygons for customGeolocation.
_STATE_POLYGONS: dict[str, Any] | None = None
_STATES_PATH = Path(__file__).parent.parent / "geo" / "us-states.json"


def _get_state_polygon(state_name: str) -> dict | None:
    """Get GeoJSON geometry for a US state. Loaded lazily."""
    global _STATE_POLYGONS
    if _STATE_POLYGONS is None:
        if _STATES_PATH.exists():
            with open(_STATES_PATH) as f:
                data = json.load(f)
            _STATE_POLYGONS = {
                feat["properties"]["NAME"].lower(): feat["geometry"]
                for feat in data.get("features", [])
            }
        else:
            _STATE_POLYGONS = {}

    return _STATE_POLYGONS.get(state_name.lower())


class ApifyScraper:
    """Wraps Apify actors for Google Maps and LinkedIn scraping."""

    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        if not self.token:
            logger.warning("No APIFY_TOKEN set — Apify scraping will fail")

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        if target.source == LeadSource.GOOGLE_MAPS:
            return await self.scrape_google_maps(target)
        elif target.source == LeadSource.LINKEDIN:
            return await self.scrape_linkedin(target)
        else:
            raise ValueError(f"Apify doesn't handle source: {target.source}")

    async def scrape_google_maps(self, target: ScrapeTarget) -> list[Lead]:
        """Scrape Google Maps. Uses polygon for US states (10-30x more results)."""
        logger.info("Apify Google Maps: %s (location: %s)", target.query, target.location)

        run_input: dict[str, Any] = {
            "searchStringsArray": [target.query],
            "language": "en",
            "scrapeContacts": True,
            "skipClosedPlaces": True,
        }

        if target.max_results > 0:
            run_input["maxCrawledPlacesPerSearch"] = target.max_results

        # Try polygon first (US states), fall back to text.
        state = target.extra.get("tile_state", "")
        country = target.extra.get("tile_country", "")
        polygon = None
        if country == "US" and state:
            # Map state code to full name for polygon lookup.
            from suplex.geo.tiles import STATE_CODES
            code_to_name = {v: k.title() for k, v in STATE_CODES.items() if len(k) > 2}
            state_name = code_to_name.get(state, state)
            polygon = _get_state_polygon(state_name)

        if polygon:
            run_input["customGeolocation"] = polygon
            logger.info("Using polygon for %s", state)
        elif target.location:
            run_input["locationQuery"] = target.location

        items = await self._run_actor(GOOGLE_MAPS_ACTOR, run_input)
        return [self._maps_item_to_lead(item) for item in items]

    async def scrape_linkedin(self, target: ScrapeTarget) -> list[Lead]:
        """Scrape LinkedIn for people matching search criteria."""
        logger.info("Apify LinkedIn: %s", target.query)

        run_input: dict[str, Any] = {
            "searchUrl": target.query,  # LinkedIn search URL or keyword.
            "maxResults": target.max_results,
        }
        if target.location:
            run_input["location"] = target.location

        run_input.update(target.extra)

        items = await self._run_actor(LINKEDIN_PEOPLE_ACTOR, run_input)
        return [self._linkedin_item_to_lead(item) for item in items]

    async def _run_actor(self, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """Run an Apify actor and wait for results."""
        client = ApifyClientAsync(self.token)
        run = await client.actor(actor_id).call(run_input=run_input)
        dataset_id = run["defaultDatasetId"]
        items_page = await client.dataset(dataset_id).list_items()
        items = items_page.items
        logger.info("Apify actor %s returned %d items", actor_id, len(items))
        return items

    @staticmethod
    def _maps_item_to_lead(item: dict[str, Any]) -> Lead:
        return Lead(
            name=item.get("title") or item.get("name"),
            email=_first_truthy(item.get("email"), item.get("emails", [None])[0] if item.get("emails") else None),
            company=item.get("title"),
            phone=item.get("phone"),
            location=item.get("address") or item.get("city"),
            website=item.get("website"),
            google_maps_url=item.get("url"),
            source=LeadSource.GOOGLE_MAPS,
            raw_data=item,
        )

    @staticmethod
    def _linkedin_item_to_lead(item: dict[str, Any]) -> Lead:
        return Lead(
            name=item.get("fullName") or item.get("name"),
            email=item.get("email"),
            company=item.get("companyName") or item.get("company"),
            title=item.get("title") or item.get("headline"),
            location=item.get("location"),
            linkedin_url=item.get("profileUrl") or item.get("url"),
            source=LeadSource.LINKEDIN,
            raw_data=item,
        )


def _first_truthy(*values: Any) -> Any:
    for v in values:
        if v:
            return v
    return None
