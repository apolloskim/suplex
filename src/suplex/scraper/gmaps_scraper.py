"""Google Maps scraper via Playwright — local, free alternative to Apify.

Ported from suplex-desktop's googleMapsPlaywright.ts.
Searches Google Maps, scrolls results, extracts business details.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from suplex.models import Lead, LeadSource, ScrapeTarget

logger = logging.getLogger(__name__)

MAPS_URL = "https://www.google.com/maps/search/{query}"
MAPS_COORD_URL = "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z"
DEFAULT_ZOOM = 13  # ~10km radius viewport.

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Selectors (from suplex-desktop).
SEL_FEED = 'div[role="feed"]'
SEL_PLACE_LINKS = 'a[href*="/maps/place"]'
SEL_TITLE = "h1.DUwDvf, h1.fontHeadlineLarge"
SEL_RATING = 'div.F7nice span[aria-hidden="true"]'
SEL_REVIEW_COUNT = "div.F7nice"
SEL_CATEGORY = 'button[jsaction*="category"]'
SEL_PHONE = 'button[data-item-id*="phone"] div.fontBodyMedium'
SEL_ADDRESS = 'button[data-item-id="address"] div.fontBodyMedium'
SEL_WEBSITE = 'a[data-item-id="authority"]'
SEL_PRICE = 'span[aria-label*="Price"]'
SEL_HOURS = '[aria-label*="hour"], [aria-label*="Hours"]'

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
REVIEW_COUNT_RE = re.compile(r"([\d,]+)\s*review", re.IGNORECASE)


class GMapsScraper:
    """Scrape Google Maps search results via Playwright."""

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        query = target.query
        max_results = target.max_results

        # Use coordinate-based URL if tile has lat/lng (more precise geographic targeting).
        lat = target.extra.get("tile_lat")
        lng = target.extra.get("tile_lng")
        if lat and lng:
            zoom = target.extra.get("tile_zoom", DEFAULT_ZOOM)
            search_query = quote_plus(query)
        elif target.location:
            query = f"{query} in {target.location}"
            lat = lng = zoom = None
            search_query = None

        logger.info("Google Maps Playwright: %s (max %d)", query, max_results)

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            geolocation=None,
            permissions=[],
        )

        leads: list[Lead] = []

        try:
            page = await context.new_page()

            # Navigate to Google Maps search.
            if lat and lng:
                url = MAPS_COORD_URL.format(
                    query=search_query, lat=lat, lng=lng, zoom=zoom,
                )
            else:
                url = MAPS_URL.format(query=quote_plus(query))
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)

            # Accept cookies/consent if present.
            await self._dismiss_consent(page)

            # Wait for results feed.
            try:
                await page.wait_for_selector(SEL_FEED, timeout=10_000)
            except Exception:
                logger.warning("No results feed found for query: %s", query)
                return leads

            # Scroll and collect place links — exhaust all results.
            place_urls = await self._scroll_and_collect(page, max_results)
            logger.info("Found %d place links", len(place_urls))

            # Visit each place and extract details.
            for i, place_url in enumerate(place_urls):
                try:
                    detail = await self._extract_place(page, place_url)
                    if detail:
                        lead = self._to_lead(detail, query)
                        leads.append(lead)
                        if (i + 1) % 10 == 0:
                            logger.info("Extracted %d/%d places", i + 1, len(place_urls))
                except Exception:
                    logger.debug("Failed to extract place: %s", place_url, exc_info=True)

            logger.info("Scraped %d leads from Google Maps", len(leads))
        finally:
            await context.close()
            await browser.close()
            await pw.stop()

        return leads

    @staticmethod
    async def _dismiss_consent(page: Page) -> None:
        """Dismiss Google consent/cookie dialogs (multi-region)."""
        # Try iframe-based consent first (common in EU).
        for frame in page.frames:
            for selector in (
                'button[aria-label="Accept all"]',
                'button:has-text("Accept all")',
                'button:has-text("Alle akzeptieren")',
                'button:has-text("Tout accepter")',
                'button:has-text("Aceptar todo")',
                'button:has-text("Accetta tutto")',
                'button:has-text("Reject all")',
                'button:has-text("Alle ablehnen")',
                'form[action*="consent"] button',
            ):
                try:
                    btn = frame.locator(selector).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click()
                        await page.wait_for_timeout(1000)
                        return
                except Exception:
                    pass

    @staticmethod
    async def _scroll_and_collect(page: Page, target_count: int) -> list[str]:
        """Scroll the results feed and collect place URLs.

        If target_count > 0, stop after collecting that many.
        If target_count == 0, scroll until Google runs out of results.
        """
        seen_urls: dict[str, None] = {}  # Ordered set via dict.
        stale_count = 0
        # Google Maps shows max ~120 results per query. 200 scrolls is generous.
        max_scrolls = target_count * 3 if target_count > 0 else 200

        feed = page.locator(SEL_FEED)

        for _ in range(max_scrolls):
            # Collect current links.
            links = await page.query_selector_all(SEL_PLACE_LINKS)
            for link in links:
                href = await link.get_attribute("href")
                if href and href not in seen_urls:
                    seen_urls[href] = None

            if target_count > 0 and len(seen_urls) >= target_count:
                break

            # Scroll the feed.
            prev_count = len(seen_urls)
            await feed.evaluate("el => el.scrollBy(0, el.clientHeight * 0.8)")
            await page.wait_for_timeout(1500)

            # Check for "end of results" text.
            end_marker = page.locator("text=You've reached the end of the list")
            try:
                if await end_marker.is_visible(timeout=500):
                    break
            except Exception:
                pass

            # Stale detection.
            if len(seen_urls) == prev_count:
                stale_count += 1
                if stale_count >= 5:
                    break
            else:
                stale_count = 0

        return list(seen_urls.keys())

    @staticmethod
    async def _extract_place(page: Page, url: str) -> dict[str, Any] | None:
        """Navigate to a place and extract its details."""
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        # Title.
        title_el = await page.query_selector(SEL_TITLE)
        if not title_el:
            return None
        title = (await title_el.inner_text()).strip()
        if not title:
            return None

        detail: dict[str, Any] = {"title": title, "url": url}

        # Rating.
        rating_el = await page.query_selector(SEL_RATING)
        if rating_el:
            try:
                detail["rating"] = float((await rating_el.inner_text()).strip().replace(",", "."))
            except ValueError:
                pass

        # Review count.
        review_el = await page.query_selector(SEL_REVIEW_COUNT)
        if review_el:
            text = await review_el.inner_text()
            match = REVIEW_COUNT_RE.search(text)
            if match:
                detail["review_count"] = int(match.group(1).replace(",", ""))

        # Category.
        cat_el = await page.query_selector(SEL_CATEGORY)
        if cat_el:
            detail["category"] = (await cat_el.inner_text()).strip()

        # Phone.
        phone_el = await page.query_selector(SEL_PHONE)
        if phone_el:
            detail["phone"] = (await phone_el.inner_text()).strip()

        # Address.
        addr_el = await page.query_selector(SEL_ADDRESS)
        if addr_el:
            detail["address"] = (await addr_el.inner_text()).strip()

        # Website.
        web_el = await page.query_selector(SEL_WEBSITE)
        if web_el:
            href = await web_el.get_attribute("href")
            if href:
                detail["website"] = href

        # Price level.
        price_el = await page.query_selector(SEL_PRICE)
        if price_el:
            label = await price_el.get_attribute("aria-label")
            if label:
                detail["price_level"] = label

        # Hours.
        hours_el = await page.query_selector(SEL_HOURS)
        if hours_el:
            label = await hours_el.get_attribute("aria-label")
            if label:
                detail["hours"] = label

        # Place ID from URL.
        place_match = re.search(r"place/[^/]+/[^/]*!8m2!3d[^!]+!4d[^!]+", url)
        if place_match:
            detail["maps_url"] = url

        return detail

    @staticmethod
    def _to_lead(detail: dict[str, Any], query: str) -> Lead:
        return Lead(
            name=detail.get("title"),
            company=detail.get("title"),
            phone=detail.get("phone"),
            location=detail.get("address"),
            website=detail.get("website"),
            google_maps_url=detail.get("url"),
            source=LeadSource.GOOGLE_MAPS,
            raw_data={
                **detail,
                "search_query": query,
            },
        )
