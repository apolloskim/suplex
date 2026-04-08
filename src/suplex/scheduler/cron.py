"""Scheduler — runs the strategist → scraper → verifier pipeline on a cron schedule."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from suplex.ai.strategist import Strategist
from suplex.scraper.coordinator import ScrapeCoordinator
from suplex.store.db import LeadStore
from suplex.verify.verifier import EmailVerifier

logger = logging.getLogger(__name__)


async def run_pipeline(domain: str, preferences: str) -> None:
    """Full autonomous pipeline: plan → scrape → verify."""
    logger.info("Pipeline triggered for domain: %s", domain)

    store = LeadStore()
    strategist = Strategist()
    coordinator = ScrapeCoordinator(store=store)
    verifier = EmailVerifier()

    # Step 1: AI generates scrape plan.
    plan = await strategist.plan(domain=domain, preferences=preferences)
    logger.info("Plan: %s (%d targets)", plan.reasoning, len(plan.targets))

    # Step 2: Execute all scrape targets concurrently.
    leads = await coordinator.run(plan.targets)
    logger.info("Scraped %d leads", len(leads))

    # Step 3: Verify new emails.
    unverified = store.get_unverified_emails(limit=200)
    for lead in unverified:
        if lead.email and lead.id:
            result = await verifier.verify(lead.email)
            store.update_verification(lead.id, result.status)

    logger.info("Pipeline complete. Total leads in DB: %d", store.count())


def start_scheduler(
    *,
    domain: str,
    preferences: str = "",
    cron_expr: str = "0 9 * * 1",
) -> None:
    """Start the APScheduler loop. Blocks until interrupted."""

    async def _main() -> None:
        scheduler = AsyncIOScheduler()

        trigger = CronTrigger.from_crontab(cron_expr)
        scheduler.add_job(
            run_pipeline,
            trigger=trigger,
            args=[domain, preferences],
            id="suplex_pipeline",
            name=f"Suplex pipeline: {domain}",
            replace_existing=True,
        )

        scheduler.start()
        logger.info("Scheduler started — cron: %s", cron_expr)

        # Run once immediately so the user sees results.
        await run_pipeline(domain, preferences)

        # Keep alive until SIGINT/SIGTERM.
        stop = asyncio.Event()

        def _handle_signal() -> None:
            logger.info("Shutting down scheduler")
            scheduler.shutdown(wait=False)
            stop.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _handle_signal)

        await stop.wait()

    asyncio.run(_main())
