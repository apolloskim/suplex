"""Enrichment pipeline — runs on unenriched leads, separate from scraping.

Steps per lead:
1. Scrape company/business website → rich data (free)
2. Extract emails from scraped site data (free)
3. Verify email via Reacher Docker (free)
4. If no email: guess via permutations + verify (free)
5. Optionally fetch LinkedIn posts (Apify, ~$0.002/lead)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from suplex.models import Lead, LeadSource, ScrapeTarget, VerificationStatus
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore
from suplex.urls import domain_of
from suplex.verify.verifier import EmailVerifier

logger = logging.getLogger(__name__)

# Person name patterns (first.last, flast, etc.)
PERSON_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}@{domain}",
    "{first}_{last}@{domain}",
    "{last}.{first}@{domain}",
    "{f}.{last}@{domain}",
]

# Role-based patterns for businesses (no person name needed).
ROLE_PATTERNS = [
    "info@{domain}",
    "contact@{domain}",
    "hello@{domain}",
    "office@{domain}",
    "admin@{domain}",
    "team@{domain}",
    "support@{domain}",
    "sales@{domain}",
]


class EnrichmentPipeline:
    """Enriches unenriched leads: site scrape + email find + verify + LinkedIn posts."""

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
        self._catch_all_cache: dict[str, bool] = {}

    async def run(self, limit: int = 100, *, job_id: str | None = None) -> dict:
        leads = self.store.get_unenriched(limit=limit)
        if not leads:
            logger.info("No unenriched leads to process")
            return {"processed": 0}

        logger.info("Enriching %d leads", len(leads))

        if job_id:
            self.store.create_job(job_id, "enrich", total=len(leads))

        stats: dict = {
            "processed": 0, "sites_scraped": 0, "emails_from_site": 0,
            "emails_verified": 0, "emails_guessed": 0, "linkedin_posts": 0,
        }

        try:
            for lead in leads:
                try:
                    enrichment = await self._enrich_one(lead, stats)
                    if lead.id:
                        self.store.update_enrichment(lead.id, enrichment)
                    stats["processed"] += 1
                    if job_id and stats["processed"] % 5 == 0:
                        self.store.update_job_progress(job_id, stats["processed"], stats["processed"])
                    if stats["processed"] % 10 == 0:
                        logger.info("Enriched %d/%d leads", stats["processed"], len(leads))
                except Exception:
                    logger.exception("Failed to enrich lead %s (id=%s)", lead.name, lead.id)

            if job_id:
                self.store.update_job_progress(job_id, len(leads), stats["processed"])
                self.store.finish_job(job_id, "done")
        except Exception as e:
            if job_id:
                self.store.finish_job(job_id, "failed", error=str(e))
            raise

        if self.verifier.setup_log:
            stats["setup_messages"] = self.verifier.setup_log

        logger.info("Enrichment complete: %s", stats)
        return stats

    async def _enrich_one(self, lead: Lead, stats: dict[str, int]) -> dict[str, Any]:
        enrichment: dict[str, Any] = {}
        found_email: str | None = lead.email

        # Step 1: Scrape website.
        website = lead.website or self._guess_website(lead)
        site_emails: list[str] = []
        if website:
            try:
                site_data = await self._scrape_website(website)
                enrichment["site"] = site_data
                stats["sites_scraped"] += 1
                site_emails = site_data.get("emails", [])
            except Exception:
                logger.debug("Site scrape failed for %s", website)

        # Step 2: Use emails from site if we don't have one.
        if not found_email and site_emails:
            found_email = site_emails[0]
            enrichment["email_source"] = "website"
            stats["emails_from_site"] += 1
            if lead.id:
                self.store.update_email(lead.id, found_email)

        # Step 3: Verify existing/found email.
        # Skip verification for LinkedIn leads — their emails are pre-verified by Apify.
        if found_email and lead.id:
            if lead.source == LeadSource.LINKEDIN and lead.verification_status != VerificationStatus.PENDING:
                enrichment["email_verification"] = {"status": lead.verification_status.value, "reason": "pre_verified_by_apify"}
                stats["emails_verified"] += 1
            else:
                try:
                    result = await self.verifier.verify(found_email)
                    self.store.update_verification(lead.id, result.status)
                    enrichment["email_verification"] = result.model_dump()
                    stats["emails_verified"] += 1
                except Exception:
                    logger.debug("Email verification failed for %s", found_email)

        # Step 4: Guess email if still none.
        if not found_email and website:
            domain = domain_of(website)
            if domain:
                guessed = await self._find_email(lead, domain, site_emails)
                if guessed and lead.id:
                    self.store.update_email(lead.id, guessed)
                    # Guessed emails were already verified by _find_email (Reacher said valid).
                    self.store.update_verification(lead.id, VerificationStatus.VALID)
                    enrichment["email_found"] = guessed
                    enrichment["email_source"] = "guessed"
                    enrichment["email_verification"] = {"status": "valid", "reason": "verified_during_guessing"}
                    stats["emails_guessed"] += 1

        # Step 5: LinkedIn posts — only for leads WITH an email (don't waste credits on unreachable leads).
        has_email = found_email or lead.email or enrichment.get("email_found")
        if self.linkedin_posts > 0 and lead.linkedin_url and self.apify_token and has_email:
            try:
                posts = await self._fetch_linkedin_posts(lead.linkedin_url)
                if posts:
                    enrichment["linkedin_posts"] = posts
                    stats["linkedin_posts"] += 1
            except Exception:
                logger.debug("LinkedIn posts fetch failed for %s", lead.linkedin_url)

        return enrichment

    async def _find_email(self, lead: Lead, domain: str, site_emails: list[str]) -> str | None:
        """Smart email finding: detect catch-all, then try patterns."""

        # Detect catch-all domain first.
        is_catch_all = await self._is_catch_all(domain)

        # Determine if this is a person (LinkedIn) or business (Google Maps).
        is_person = lead.source == LeadSource.LINKEDIN and self._has_person_name(lead.name)

        if is_person:
            # Try person name patterns.
            patterns = PERSON_PATTERNS
            parts = lead.name.strip().split()
            first = re.sub(r"[^a-z]", "", parts[0].lower())
            last = re.sub(r"[^a-z]", "", parts[-1].lower())
            if not first or not last:
                return None
            f = first[0]
            candidates = [p.format(first=first, last=last, f=f, domain=domain) for p in patterns]
        else:
            # Business: try role-based addresses.
            candidates = [p.format(domain=domain) for p in ROLE_PATTERNS]

        # If site already has emails, use the pattern from those to prioritize.
        if site_emails:
            pattern = self._detect_pattern(site_emails[0], domain)
            if pattern and is_person:
                # Reorder: put the detected pattern first.
                detected = pattern.format(first=first, last=last, f=f, domain=domain)
                if detected in candidates:
                    candidates.remove(detected)
                    candidates.insert(0, detected)

        if is_catch_all:
            # Catch-all domain: SMTP verification is useless for guessed emails.
            # Do NOT return a guess — it could be anyone or no one.
            logger.debug("Domain %s is catch-all — skipping email guessing (unreliable)", domain)
            return None

        # Verify each candidate.
        for email in candidates:
            try:
                result = await self.verifier.verify(email)
                if result.status == VerificationStatus.VALID:
                    return email
            except Exception:
                continue

        return None

    async def _is_catch_all(self, domain: str) -> bool:
        """Test if a domain is catch-all by verifying a fake address."""
        if domain in self._catch_all_cache:
            return self._catch_all_cache[domain]

        fake = f"xyzfaketest98765@{domain}"
        try:
            result = await self.verifier.verify(fake)
            is_ca = result.status in (VerificationStatus.VALID, VerificationStatus.CATCH_ALL)
            self._catch_all_cache[domain] = is_ca
            if is_ca:
                logger.debug("Domain %s is catch-all", domain)
            return is_ca
        except Exception:
            return False

    @staticmethod
    def _has_person_name(name: str | None) -> bool:
        """Check if a name looks like a person (not a business)."""
        if not name:
            return False
        # Business names often have these.
        business_words = {"dental", "dentist", "clinic", "center", "group", "associates",
                          "practice", "office", "studio", "llc", "inc", "corp", "ltd",
                          "company", "services", "solutions", "professional", "family"}
        words = name.lower().split()
        if any(w in business_words for w in words):
            return False
        # Person names are typically 2-3 words, no special chars.
        if len(words) < 2 or len(words) > 4:
            return False
        return True

    @staticmethod
    def _detect_pattern(email: str, domain: str) -> str | None:
        """Detect the email pattern from a known email address."""
        local = email.split("@")[0].lower()
        if "." in local:
            return "{first}.{last}@{domain}"
        if "_" in local:
            return "{first}_{last}@{domain}"
        return None

    async def _scrape_website(self, url: str) -> dict[str, Any]:
        target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=url)
        leads = await self.site_scraper.scrape(target)
        if leads and leads[0].raw_data:
            return leads[0].raw_data
        return {}

    async def _fetch_linkedin_posts(self, linkedin_url: str) -> list[dict]:
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
                "url": item.get("linkedinUrl") or item.get("url") or "",
                "date": item.get("postedAtISO") or item.get("postedDate", ""),
                "likes": item.get("numLikes") or item.get("likesCount", 0),
                "comments": item.get("numComments") or item.get("commentsCount", 0),
                "author": item.get("author", {}).get("name", ""),
            })
        return posts

    @staticmethod
    def _guess_website(lead: Lead) -> str | None:
        raw_website = lead.raw_data.get("companyWebsite") or lead.raw_data.get("company_website", "")
        if raw_website:
            return raw_website
        if lead.company:
            clean = re.sub(r"[^a-z0-9]", "", lead.company.lower())
            if clean:
                return f"https://{clean}.com"
        return None
