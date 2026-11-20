from __future__ import annotations

import logging
import os
import re
from typing import Any

from suplex.models import Lead, LeadSource, ScrapeTarget, VerificationStatus
from suplex.scraper.apify_runner import run_actor
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore
from suplex.urls import domain_of
from suplex.verify.verifier import EmailVerifier

logger = logging.getLogger(__name__)

LINKEDIN_POSTS_ACTOR = "harvestapi/linkedin-profile-posts"

PERSON_PATTERNS = [
    "{first}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
    "{first}@{domain}",
    "{first}_{last}@{domain}",
    "{last}.{first}@{domain}",
    "{f}.{last}@{domain}",
]

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
            if job_id:
                self.store.create_job(job_id, "enrich", total=0)
                self.store.finish_job(job_id, "done")
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

        if not found_email and site_emails:
            found_email = site_emails[0]
            enrichment["email_source"] = "website"
            stats["emails_from_site"] += 1
            if lead.id:
                self.store.update_email(lead.id, found_email)

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

        if not found_email and website:
            domain = domain_of(website)
            if domain:
                guessed = await self._find_email(lead, domain, site_emails)
                if guessed and lead.id:
                    self.store.update_email(lead.id, guessed)
                    self.store.update_verification(lead.id, VerificationStatus.VALID)
                    enrichment["email_found"] = guessed
                    enrichment["email_source"] = "guessed"
                    enrichment["email_verification"] = {"status": "valid", "reason": "verified_during_guessing"}
                    stats["emails_guessed"] += 1

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
        is_catch_all = await self._is_catch_all(domain)

        person = self._person_name_parts(lead) if lead.source == LeadSource.LINKEDIN else None
        if person:
            first, last = person
            fields = {"first": first, "last": last, "f": first[0], "domain": domain}
            candidates = [p.format(**fields) for p in PERSON_PATTERNS]
            pattern = self._detect_pattern(site_emails[0], domain) if site_emails else None
            if pattern:
                detected = pattern.format(**fields)
                if detected in candidates:
                    candidates.remove(detected)
                    candidates.insert(0, detected)
        elif lead.source == LeadSource.LINKEDIN and self._has_person_name(lead.name):
            return None
        else:
            candidates = [p.format(domain=domain) for p in ROLE_PATTERNS]

        if is_catch_all:
            logger.debug("Domain %s is catch-all — skipping email guessing (unreliable)", domain)
            return None

        for email in candidates:
            try:
                result = await self.verifier.verify(email)
                if result.status == VerificationStatus.VALID:
                    return email
            except Exception:
                continue

        return None

    async def _is_catch_all(self, domain: str) -> bool:
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

    @classmethod
    def _person_name_parts(cls, lead: Lead) -> tuple[str, str] | None:
        if not lead.name or not cls._has_person_name(lead.name):
            return None
        parts = lead.name.strip().split()
        first = re.sub(r"[^a-z]", "", parts[0].lower())
        last = re.sub(r"[^a-z]", "", parts[-1].lower())
        return (first, last) if first and last else None

    @staticmethod
    def _has_person_name(name: str | None) -> bool:
        if not name:
            return False
        business_words = {"dental", "dentist", "clinic", "center", "group", "associates",
                          "practice", "office", "studio", "llc", "inc", "corp", "ltd",
                          "company", "services", "solutions", "professional", "family"}
        words = name.lower().split()
        if any(w in business_words for w in words):
            return False
        if len(words) < 2 or len(words) > 4:
            return False
        return True

    @staticmethod
    def _detect_pattern(email: str, domain: str) -> str | None:
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
        items = await run_actor(
            self.apify_token,
            LINKEDIN_POSTS_ACTOR,
            {"profileUrls": [linkedin_url], "maxItems": self.linkedin_posts},
        )

        posts = []
        for item in items:
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
