"""Scrape coordinator — runs scrapers concurrently with resource limits, job tracking, and crash recovery."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import anyio

from suplex.apify_budget import get_apify_budget
from suplex.geo.tiles import filter_tiles, resolve_tiles
from suplex.models import Lead, LeadSource, ScrapeTarget
from suplex.scraper.apify_scraper import ApifyScraper
from suplex.scraper.gmaps_scraper import GMapsScraper
from suplex.scraper.linkedin_scraper import LinkedInScraper
from suplex.scraper.site_scraper import SiteScraper
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

DEFAULT_BROWSER_CONCURRENCY = 3
DEFAULT_API_CONCURRENCY = 10
BUDGET_CACHE_SECONDS = 60
MAX_ERROR_LENGTH = 300

Collector = Callable[[str, list[Lead]], Awaitable[None]]


class ScrapeConfigError(ValueError):
    """The requested scrape cannot run with the current configuration."""


class ApifyBudgetExhausted(RuntimeError):
    """The Apify account has no remaining monthly budget."""


class ScrapeCoordinator:
    """Orchestrates concurrent scraping with resource bounds, job tracking, and crash recovery.

    ``scraper`` picks the engine for Google Maps targets:
      - "playwright": local browser (free, slower) — the default
      - "apify": Apify actor (paid, faster, more results)
    LinkedIn always goes through Apify; websites always use the site scraper.

    After ``run`` returns, ``failed_targets`` maps the item key of every target
    that failed in that run to a short reason.
    """

    def __init__(
        self,
        store: LeadStore,
        *,
        browser_concurrency: int = DEFAULT_BROWSER_CONCURRENCY,
        api_concurrency: int = DEFAULT_API_CONCURRENCY,
        apify_token: str | None = None,
        scraper: str = "playwright",
        reviews_per_place: int = 5,
    ) -> None:
        self.store = store
        self.browser_limiter = anyio.CapacityLimiter(browser_concurrency)
        self.api_limiter = anyio.CapacityLimiter(api_concurrency)
        self.site_scraper = SiteScraper()
        self.apify_scraper = ApifyScraper(token=apify_token)
        self.gmaps_scraper = GMapsScraper(reviews_per_place=reviews_per_place)
        self.linkedin_scraper = LinkedInScraper(token=apify_token, store=store)
        self._apify_token = apify_token or ""
        self._scraper = scraper
        self._budget_lock = asyncio.Lock()
        self._budget_ok = True
        self._budget_checked_at: float | None = None
        self.failed_targets: dict[str, str] = {}

    async def run(
        self,
        targets: list[ScrapeTarget],
        *,
        job_id: str | None = None,
    ) -> list[Lead]:
        """Run all scrape targets concurrently. Resumable via job_id."""
        self._check_configuration(targets)
        self.failed_targets = {}

        expanded = self._expand(targets)
        if len(expanded) != len(targets):
            logger.info("Expanded %d targets → %d tiles", len(targets), len(expanded))

        # Seed item tasks for crash recovery.
        if job_id:
            self.store.create_job(job_id, "scrape", total=len(expanded))
            self.store.seed_item_tasks(job_id, [_item_key(t) for t in expanded])
            # Reset any stale tasks from a previous crashed run.
            reset = self.store.reset_stale_tasks(job_id)
            if reset:
                logger.info("Reset %d stale tasks from previous run", reset)

        all_leads: list[Lead] = []
        completed_count = 0
        lock = asyncio.Lock()

        async def _collect(item_key: str, leads: list[Lead]) -> None:
            nonlocal completed_count
            async with lock:
                all_leads.extend(leads)
                self.store.insert_leads(leads)
                completed_count += 1
                if job_id:
                    self.store.complete_item_task(job_id, item_key)
                    if completed_count % 5 == 0:
                        self.store.update_job_progress(job_id, completed_count, len(all_leads))

        try:
            async with anyio.create_task_group() as tg:
                for target in expanded:
                    item_key = _item_key(target)
                    # Skip already-done items (crash recovery).
                    if job_id and self.store.is_item_done(job_id, item_key):
                        completed_count += 1
                        continue
                    tg.start_soon(self._run_target, target, item_key, _collect, job_id)
        except Exception as e:
            if job_id:
                self.store.finish_job(job_id, "failed", error=str(e)[:MAX_ERROR_LENGTH])
            raise

        if job_id:
            self.store.update_job_progress(job_id, completed_count, len(all_leads))
            self._finish_job(job_id, total=len(expanded))

        if self.failed_targets:
            logger.warning(
                "%d of %d targets failed (first reason: %s)",
                len(self.failed_targets), len(expanded), next(iter(self.failed_targets.values())),
            )
        logger.info("Scraped %d leads from %d targets", len(all_leads), len(expanded))
        return all_leads

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _check_configuration(self, targets: list[ScrapeTarget]) -> None:
        """Fail before any work starts if a target needs Apify and there is no token."""
        if self._apify_token:
            return
        sources = {t.source for t in targets}
        if LeadSource.LINKEDIN in sources:
            raise ScrapeConfigError(
                "LinkedIn scraping runs on Apify and no Apify token is configured. "
                "Run `suplex setup` or set APIFY_TOKEN."
            )
        if LeadSource.GOOGLE_MAPS in sources and self._scraper == "apify":
            raise ScrapeConfigError(
                "The Apify scraper was requested for Google Maps but no Apify token is configured. "
                "Run `suplex setup`, set APIFY_TOKEN, or use `-s playwright`."
            )

    def _expand(self, targets: list[ScrapeTarget]) -> list[ScrapeTarget]:
        """Turn each located Maps/LinkedIn target into per-city tiles and apply region filters."""
        expanded: list[ScrapeTarget] = []
        for target in targets:
            if target.source not in (LeadSource.GOOGLE_MAPS, LeadSource.LINKEDIN) or not target.location:
                expanded.append(target)
                continue

            tiles = resolve_tiles(
                target.query, target.location,
                max_results_per_tile=target.max_results,
                source=target.source, store=self.store,
            )
            # Carry the original target's options onto each tile (tile-specific keys win).
            for tile in tiles:
                tile.extra = {**target.extra, **tile.extra}

            expanded.extend(filter_tiles(
                tiles,
                only=target.extra.get("only_states"),
                exclude=target.extra.get("exclude_states"),
            ))
        return expanded

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _run_target(
        self, target: ScrapeTarget, item_key: str,
        collect: Collector, job_id: str | None,
    ) -> None:
        try:
            if target.source == LeadSource.PLAYWRIGHT:
                async with self.browser_limiter:
                    leads = await self.site_scraper.scrape(target)
            elif target.source == LeadSource.GOOGLE_MAPS and self._scraper == "apify":
                await self._require_apify_budget()
                async with self.api_limiter:
                    leads = await self.apify_scraper.scrape(target)
            elif target.source == LeadSource.GOOGLE_MAPS:
                async with self.browser_limiter:
                    leads = await self.gmaps_scraper.scrape(target)
            elif target.source == LeadSource.LINKEDIN:
                await self._require_apify_budget()
                async with self.api_limiter:
                    leads = await self.linkedin_scraper.scrape(target)
            else:
                raise ValueError(f"No scraper for source: {target.source}")
            await collect(item_key, leads)
        except ApifyBudgetExhausted as exc:
            logger.warning("Skipping %s: %s", item_key, exc)
            self._record_failure(job_id, item_key, "apify_budget_exhausted")
        except Exception as exc:
            logger.exception("Failed target: %s", item_key)
            self._record_failure(job_id, item_key, f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH])

    def _record_failure(self, job_id: str | None, item_key: str, reason: str) -> None:
        self.failed_targets[item_key] = reason
        if job_id:
            self.store.fail_item_task(job_id, item_key, reason)

    def _finish_job(self, job_id: str, *, total: int) -> None:
        """Close the job row. A job with failed tiles is not plain "done" — it can be resumed."""
        if not self.failed_targets:
            self.store.finish_job(job_id, "done")
            return
        first_reason = next(iter(self.failed_targets.values()))
        self.store.finish_job(
            job_id,
            "done_with_errors",
            error=f"{len(self.failed_targets)} of {total} targets failed; first: {first_reason}"[:MAX_ERROR_LENGTH],
        )

    async def _require_apify_budget(self) -> None:
        """Raise ApifyBudgetExhausted if the Apify account is over its monthly limit.

        The answer is cached for a minute and guarded by a lock, so a job with
        hundreds of tiles asks Apify once rather than once per tile.
        """
        async with self._budget_lock:
            now = time.monotonic()
            stale = self._budget_checked_at is None or now - self._budget_checked_at >= BUDGET_CACHE_SECONDS
            if stale:
                self._budget_ok = await self._fetch_budget_ok()
                self._budget_checked_at = now
            if not self._budget_ok:
                raise ApifyBudgetExhausted("Apify monthly budget is used up")

    async def _fetch_budget_ok(self) -> bool:
        budget: dict
        try:
            budget = await get_apify_budget(self._apify_token)
        except Exception as exc:
            budget = {"error": str(exc)}

        if budget.get("error"):
            # Unknown is not the same as exhausted: let the run proceed and let
            # Apify itself reject it if the account really is over its limit.
            logger.warning("Could not check Apify budget (%s) — continuing", budget["error"])
            return True

        ok = not budget.get("over_limit", False) and budget.get("remaining_usd", 0) > 0
        if not ok:
            logger.warning(
                "Apify budget: $%.2f used / $%.2f limit — over budget",
                budget.get("used_usd", 0), budget.get("plan_limit_usd", 0),
            )
        return ok


def _item_key(target: ScrapeTarget) -> str:
    """Stable identifier for one unit of work inside a job (used for resume)."""
    return f"{target.source.value}:{target.location}:{target.query}"
