"""Apify-based scrapers for Google Maps and LinkedIn."""

from __future__ import annotations

import logging
import os
from typing import Any

from apify_client import ApifyClientAsync

from suplex.models import Lead, LeadSource, ScrapeTarget

logger = logging.getLogger(__name__)

# Apify actor IDs.
GOOGLE_MAPS_ACTOR = "compass/crawler-google-places"
LINKEDIN_PEOPLE_ACTOR = "curious_coder/linkedin-search-scraper"


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
        """Scrape Google Maps for business leads."""
        logger.info("Apify Google Maps: %s (location: %s)", target.query, target.location)

        run_input: dict[str, Any] = {
            "searchStringsArray": [target.query],
            "maxCrawledPlacesPerSearch": target.max_results,
            "language": "en",
            "scrapeContacts": True,
            "scrapeReviewsPersonalData": False,
        }
        if target.location:
            run_input["locationQuery"] = target.location

        run_input.update(target.extra)

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
        async with ApifyClientAsync(self.token) as client:
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
