"""Playwright-based local browser scraper for extracting contacts from websites."""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from playwright.async_api import async_playwright, Browser, Page

from suplex.models import Lead, LeadSource, ScrapeTarget

logger = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"[\+]?[(]?[0-9]{1,4}[)]?[-\s\./0-9]{7,15}")

# Block these resource types to save CPU/bandwidth.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet"}


class PlaywrightScraper:
    """Scrapes websites with Playwright, extracting contact info from page content."""

    def __init__(self) -> None:
        self._browser: Browser | None = None

    async def _ensure_browser(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            pw = await async_playwright().start()
            self._browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--no-sandbox",
                ],
            )
        return self._browser

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        """Scrape a website URL and extract leads from it."""
        url = target.query  # For playwright targets, query is the URL.
        logger.info("Playwright scraping: %s", url)

        browser = await self._ensure_browser()
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 720},
        )

        try:
            page = await context.new_page()
            await self._block_heavy_resources(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2000)  # Let JS settle.

            leads = await self._extract_leads(page, url)

            # Try contact/about pages for more leads.
            contact_urls = await self._find_contact_pages(page, url)
            for contact_url in contact_urls[:3]:
                try:
                    await page.goto(contact_url, wait_until="domcontentloaded", timeout=20_000)
                    await page.wait_for_timeout(1000)
                    leads.extend(await self._extract_leads(page, contact_url))
                except Exception:
                    logger.debug("Failed to scrape contact page: %s", contact_url)

            # Deduplicate by email.
            seen_emails: set[str] = set()
            unique: list[Lead] = []
            for lead in leads:
                if lead.email and lead.email not in seen_emails:
                    seen_emails.add(lead.email)
                    unique.append(lead)
                elif not lead.email:
                    unique.append(lead)

            logger.info("Extracted %d leads from %s", len(unique), url)
            return unique
        finally:
            await context.close()

    async def close(self) -> None:
        if self._browser and self._browser.is_connected():
            await self._browser.close()
            self._browser = None

    @staticmethod
    async def _block_heavy_resources(page: Page) -> None:
        async def _handle_route(route):
            if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _handle_route)

    @staticmethod
    async def _extract_leads(page: Page, source_url: str) -> list[Lead]:
        text = await page.inner_text("body")
        title = await page.title()

        emails = list(set(EMAIL_RE.findall(text)))
        phones = list(set(PHONE_RE.findall(text)))

        leads: list[Lead] = []
        for i, email in enumerate(emails):
            # Skip obvious non-person emails.
            if any(skip in email.lower() for skip in ["noreply", "no-reply", "example.com"]):
                continue
            leads.append(
                Lead(
                    email=email,
                    phone=phones[i] if i < len(phones) else None,
                    company=title,
                    website=source_url,
                    source=LeadSource.PLAYWRIGHT,
                    raw_data={"page_title": title, "source_url": source_url},
                )
            )

        return leads

    @staticmethod
    async def _find_contact_pages(page: Page, base_url: str) -> list[str]:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent.toLowerCase()}))",
        )
        contact_urls: list[str] = []
        for link in links:
            text = link.get("text", "")
            href = link.get("href", "")
            if any(kw in text for kw in ["contact", "about", "team", "staff"]):
                full_url = urljoin(base_url, href)
                if full_url.startswith("http") and full_url not in contact_urls:
                    contact_urls.append(full_url)
        return contact_urls
