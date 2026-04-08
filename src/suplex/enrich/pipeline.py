"""Enrichment pipeline — runs on unenriched leads, separate from scraping.

Steps per lead:
1. Scrape company/business website → rich data (site_scraper, free)
2. Verify email → Reacher Docker (free)
3. If no email + have name + domain: guess email permutations → verify (free)
4. Optionally fetch LinkedIn posts → Apify ($0.002/lead)

Each step is independent — if one fails, the others still run.
The pipeline picks up where it left off (only processes unenriched leads).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import anyio

from suplex.models import Lead, LeadSource, ScrapeTarget, VerificationStatus
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore
from suplex.verify.verifier import EmailVerifier

logger = logging.getLogger(__name__)

# Common email patterns, ordered by likelihood.
EMAIL_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}@{domain}",
    "{first}_{last}@{domain}",
    "{last}.{first}@{domain}",
    "{f}.{last}@{domain}",
    "{last}@{domain}",
]


class EnrichmentPipeline:
    """Enriches unenriched leads: site scrape + email verify + email guess + LinkedIn posts."""

    def __init__(
        self,
        store: LeadStore | None = None,
        *,
        apify_token: str | None = None,
        linkedin_posts: int = 1,
    ) -> None:
        self.store = store or LeadStore()
        self.site_scraper = SiteScraper()
        self.verifier = EmailVerifier()
        self.apify_token = apify_token or os.environ.get("APIFY_TOKEN", "")
        self.linkedin_posts = linkedin_posts

    async def run(self, limit: int = 100) -> dict[str, int]:
        """Enrich up to `limit` unenriched leads. Returns stats."""
        leads = self.store.get_unenriched(limit=limit)
        if not leads:
            logger.info("No unenriched leads to process")
            return {"processed": 0}

        logger.info("Enriching %d leads", len(leads))

        stats = {"processed": 0, "sites_scraped": 0, "emails_verified": 0, "emails_found": 0, "linkedin_posts": 0}

        for lead in leads:
            try:
                enrichment = await self._enrich_one(lead, stats)
                if lead.id:
                    self.store.update_enrichment(lead.id, enrichment)
                stats["processed"] += 1
                if stats["processed"] % 10 == 0:
                    logger.info("Enriched %d/%d leads", stats["processed"], len(leads))
            except Exception:
                logger.exception("Failed to enrich lead %s (id=%s)", lead.name, lead.id)

        logger.info("Enrichment complete: %s", stats)
        return stats

    async def _enrich_one(self, lead: Lead, stats: dict[str, int]) -> dict[str, Any]:
        """Enrich a single lead. Returns enrichment data dict."""
        enrichment: dict[str, Any] = {}

        # Step 1: Scrape website (if available).
        website = lead.website or self._guess_website(lead)
        if website:
            try:
                site_data = await self._scrape_website(website)
                enrichment["site"] = site_data
                stats["sites_scraped"] += 1
            except Exception:
                logger.debug("Site scrape failed for %s", website)

        # Step 2: Verify existing email.
        if lead.email and lead.id:
            try:
                result = await self.verifier.verify(lead.email)
                self.store.update_verification(lead.id, result.status)
                enrichment["email_verification"] = result.model_dump()
                stats["emails_verified"] += 1
            except Exception:
                logger.debug("Email verification failed for %s", lead.email)

        # Step 3: Find email if we don't have one.
        if not lead.email and lead.name and website:
            domain = self._extract_domain(website)
            if domain:
                found = await self._guess_email(lead.name, domain)
                if found and lead.id:
                    self.store.update_email(lead.id, found)
                    enrichment["email_found"] = found
                    stats["emails_found"] += 1

        # Step 4: Fetch LinkedIn posts (if enabled and URL available).
        if self.linkedin_posts > 0 and lead.linkedin_url and self.apify_token:
            try:
                posts = await self._fetch_linkedin_posts(lead.linkedin_url)
                if posts:
                    enrichment["linkedin_posts"] = posts
                    stats["linkedin_posts"] += 1
            except Exception:
                logger.debug("LinkedIn posts fetch failed for %s", lead.linkedin_url)

        return enrichment

    async def _scrape_website(self, url: str) -> dict[str, Any]:
        """Scrape a website and return the rich site data."""
        target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=url)
        leads = await self.site_scraper.scrape(target)
        if leads and leads[0].raw_data:
            return leads[0].raw_data
        return {}

    async def _guess_email(self, name: str, domain: str) -> str | None:
        """Generate email permutations from name + domain, verify each with Reacher."""
        parts = name.strip().split()
        if len(parts) < 2:
            return None

        first = parts[0].lower()
        last = parts[-1].lower()
        f = first[0]

        # Clean non-alpha characters.
        first = re.sub(r"[^a-z]", "", first)
        last = re.sub(r"[^a-z]", "", last)
        if not first or not last:
            return None

        for pattern in EMAIL_PATTERNS:
            email = pattern.format(first=first, last=last, f=f, domain=domain)
            try:
                result = await self.verifier.verify(email)
                if result.status == VerificationStatus.VALID:
                    logger.info("Found email: %s", email)
                    return email
            except Exception:
                continue

        return None

    async def _fetch_linkedin_posts(self, linkedin_url: str) -> list[dict]:
        """Fetch recent LinkedIn posts via Apify."""
        from apify_client import ApifyClientAsync

        client = ApifyClientAsync(self.apify_token)
        run = await client.actor("harvestapi/linkedin-profile-posts").call(
            run_input={
                "profileUrls": [linkedin_url],
                "maxItems": self.linkedin_posts,
            },
        )
        dataset_id = run["defaultDatasetId"]
        items_page = await client.dataset(dataset_id).list_items()

        posts = []
        for item in items_page.items:
            text = item.get("content") or item.get("text") or item.get("commentary") or ""
            posts.append({
                "text": text[:500],
                "url": item.get("linkedinUrl") or item.get("url") or item.get("postUrl", ""),
                "date": item.get("postedAtISO") or item.get("postedDate", ""),
                "likes": item.get("numLikes") or item.get("likesCount", 0),
                "comments": item.get("numComments") or item.get("commentsCount", 0),
                "author": item.get("author", {}).get("name", ""),
            })

        return posts

    @staticmethod
    def _guess_website(lead: Lead) -> str | None:
        """Try to guess a website from company name or raw data."""
        # Check raw_data for any website hints.
        raw_website = lead.raw_data.get("companyWebsite") or lead.raw_data.get("company_website", "")
        if raw_website:
            return raw_website

        # For LinkedIn leads, try company name as domain.
        if lead.company:
            clean = re.sub(r"[^a-z0-9]", "", lead.company.lower())
            if clean:
                return f"https://{clean}.com"

        return None

    @staticmethod
    def _extract_domain(url: str) -> str | None:
        """Extract domain from URL."""
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            host = parsed.netloc or parsed.path
            host = host.lstrip("www.")
            return host if "." in host else None
        except Exception:
            return None
