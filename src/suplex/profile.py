from __future__ import annotations

import logging
from typing import Any

from suplex.models import LeadSource, ScrapeTarget
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)


class SenderProfile:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {}

    @property
    def website(self) -> str:
        return self.data.get("website", "")

    @property
    def business_name(self) -> str:
        return self.data.get("business_name", "")

    @property
    def description(self) -> str:
        return self.data.get("description", "")

    @property
    def clean_text(self) -> str:
        return self.data.get("clean_text", "")

    @property
    def is_configured(self) -> bool:
        return bool(self.data and self.website)

    def summary(self) -> dict[str, Any]:
        if not self.is_configured:
            return {"configured": False}
        return {
            "configured": True,
            "website": self.website,
            "business_name": self.business_name,
            "description": self.description[:200] if self.description else "",
            "emails": self.data.get("emails", []),
            "phones": self.data.get("phones", []),
            "tech_stack": self.data.get("tech_stack", []),
        }

    @classmethod
    def load(cls, store: LeadStore | None = None) -> SenderProfile:
        store = store or LeadStore()
        data = store.get_profile()
        return cls(data)


async def scrape_sender_profile(url: str) -> SenderProfile:
    scraper = SiteScraper()
    target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=url)
    leads = await scraper.scrape(target)

    if not leads:
        raise ValueError(f"Could not scrape any data from {url}")

    raw = leads[0].raw_data
    raw["website"] = url

    store = LeadStore()
    store.save_profile(url, raw)

    profile = SenderProfile(raw)
    logger.info("Sender profile saved: %s", profile.business_name)
    return profile
