from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from suplex.geo.tiles import STATE_NAMES
from suplex.models import Lead, LeadSource, ScrapeTarget
from suplex.scraper.apify_runner import run_actor

logger = logging.getLogger(__name__)

GOOGLE_MAPS_ACTOR = "compass/crawler-google-places"

_STATE_POLYGONS: dict[str, Any] | None = None
_STATES_PATH = Path(__file__).parent.parent / "geo" / "us-states.json"


def _get_state_polygon(state_name: str) -> dict | None:
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
    def __init__(self, token: str | None = None) -> None:
        self.token = token or os.environ.get("APIFY_TOKEN", "")

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        if target.source != LeadSource.GOOGLE_MAPS:
            raise ValueError(f"ApifyScraper only handles Google Maps, not {target.source.value}")
        return await self.scrape_google_maps(target)

    async def scrape_google_maps(self, target: ScrapeTarget) -> list[Lead]:
        logger.info("Apify Google Maps: %s (location: %s)", target.query, target.location)

        run_input: dict[str, Any] = {
            "searchStringsArray": [target.query],
            "language": "en",
            "scrapeContacts": True,
            "skipClosedPlaces": True,
        }

        if target.max_results > 0:
            run_input["maxCrawledPlacesPerSearch"] = target.max_results

        state = target.extra.get("tile_state", "")
        country = target.extra.get("tile_country", "")
        polygon = None
        if country == "US" and state:
            polygon = _get_state_polygon(STATE_NAMES.get(state) or state)

        if polygon:
            run_input["customGeolocation"] = polygon
            logger.info("Using polygon for %s", state)
        elif target.location:
            run_input["locationQuery"] = target.location

        items = await run_actor(self.token, GOOGLE_MAPS_ACTOR, run_input)
        return [self._maps_item_to_lead(item) for item in items]

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


def _first_truthy(*values: Any) -> Any:
    for v in values:
        if v:
            return v
    return None
