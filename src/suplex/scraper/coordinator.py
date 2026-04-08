"""Scrape coordinator — runs scrapers concurrently with resource limits and job tracking."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import anyio
import psutil

from suplex.geo.tiles import resolve_tiles
from suplex.models import Lead, ScrapeTarget, LeadSource
from suplex.scraper.apify_scraper import ApifyScraper
from suplex.scraper.gmaps_scraper import GMapsScraper
from suplex.scraper.linkedin_scraper import LinkedInScraper
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_CONCURRENCY = 3
DEFAULT_API_CONCURRENCY = 10
CPU_THROTTLE_PERCENT = 80.0


class ScrapeCoordinator:
    """Orchestrates concurrent scraping with resource bounds and job progress tracking."""

    def __init__(
        self,
        store: LeadStore,
        *,
        browser_concurrency: int = DEFAULT_BROWSER_CONCURRENCY,
        api_concurrency: int = DEFAULT_API_CONCURRENCY,
        apify_token: str | None = None,
    ) -> None:
        self.store = store
        self.browser_limiter = anyio.CapacityLimiter(browser_concurrency)
        self.api_limiter = anyio.CapacityLimiter(api_concurrency)
        self.site_scraper = SiteScraper()
        self.apify_scraper = ApifyScraper(token=apify_token)
        self.gmaps_scraper = GMapsScraper()
        self.linkedin_scraper = LinkedInScraper(token=apify_token, store=store)
        self._has_apify = bool(apify_token)

    async def run(
        self,
        targets: list[ScrapeTarget],
        *,
        job_id: str | None = None,
    ) -> list[Lead]:
        """Run all scrape targets concurrently.

        If job_id is provided, progress is tracked in the jobs table.
        """
        # Expand tiles.
        expanded: list[ScrapeTarget] = []
        for target in targets:
            if target.source in (LeadSource.GOOGLE_MAPS, LeadSource.LINKEDIN) and target.location:
                tiles = resolve_tiles(
                    target.query, target.location,
                    max_results_per_tile=target.max_results,
                    source=target.source, has_apify=self._has_apify, store=self.store,
                )
                expanded.extend(tiles)
            else:
                expanded.append(target)

        if len(expanded) != len(targets):
            logger.info("Expanded %d targets → %d tiles", len(targets), len(expanded))

        # Update job with total tile count.
        if job_id:
            self.store.create_job(job_id, "scrape", total=len(expanded))

        all_leads: list[Lead] = []
        completed_count = 0
        lock = asyncio.Lock()

        async def _collect(leads: list[Lead]) -> None:
            nonlocal completed_count
            async with lock:
                all_leads.extend(leads)
                self.store.insert_leads(leads)
                completed_count += 1
                if job_id and completed_count % 5 == 0:
                    self.store.update_job_progress(job_id, completed_count, len(all_leads))

        try:
            async with anyio.create_task_group() as tg:
                for target in expanded:
                    tg.start_soon(self._run_target, target, _collect)

            if job_id:
                self.store.update_job_progress(job_id, len(expanded), len(all_leads))
                self.store.finish_job(job_id, "done")
        except Exception as e:
            if job_id:
                self.store.finish_job(job_id, "failed", error=str(e))
            raise

        logger.info("Scraped %d leads from %d targets", len(all_leads), len(expanded))
        return all_leads

    async def _run_target(self, target: ScrapeTarget, callback: Any) -> None:
        try:
            if target.source == LeadSource.PLAYWRIGHT:
                await self._run_with_cpu_guard(
                    self.browser_limiter, self.site_scraper.scrape, target, callback,
                )
            elif target.source == LeadSource.GOOGLE_MAPS:
                use_apify = (
                    self._has_apify and target.extra.get("tile_scraper", "apify") == "apify"
                )
                if use_apify:
                    async with self.api_limiter:
                        leads = await self.apify_scraper.scrape(target)
                        await callback(leads)
                else:
                    await self._run_with_cpu_guard(
                        self.browser_limiter, self.gmaps_scraper.scrape, target, callback,
                    )
            elif target.source == LeadSource.LINKEDIN:
                async with self.api_limiter:
                    leads = await self.linkedin_scraper.scrape(target)
                    await callback(leads)
            else:
                async with self.api_limiter:
                    leads = await self.apify_scraper.scrape(target)
                    await callback(leads)
        except Exception:
            logger.exception("Failed to scrape target: %s", target.query)

    async def _run_with_cpu_guard(
        self, limiter: anyio.CapacityLimiter, fn: Any,
        target: ScrapeTarget, callback: Any,
    ) -> None:
        while psutil.cpu_percent(interval=0.5) > CPU_THROTTLE_PERCENT:
            logger.warning("CPU at %.1f%% — throttling", psutil.cpu_percent())
            await anyio.sleep(2)
        async with limiter:
            leads = await fn(target)
            await callback(leads)
