from __future__ import annotations

import logging
import random
import re
from typing import Any
from urllib.parse import quote_plus

from playwright.async_api import Page, async_playwright

from suplex.models import Lead, LeadSource, ScrapeTarget

logger = logging.getLogger(__name__)

MAPS_URL = "https://www.google.com/maps/search/{query}"
MAPS_COORD_URL = "https://www.google.com/maps/search/{query}/@{lat},{lng},{zoom}z"
DEFAULT_ZOOM = 13
DAILY_LIMIT = 1000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

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


class DailyLimitReached(RuntimeError):
    pass


class GMapsScraper:
    _daily_count: int = 0
    _daily_date: str = ""

    def __init__(self, *, reviews_per_place: int = 5) -> None:
        self.reviews_per_place = reviews_per_place

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        from datetime import date
        today = date.today().isoformat()
        if self._daily_date != today:
            GMapsScraper._daily_date = today
            GMapsScraper._daily_count = 0
        if self._daily_count >= DAILY_LIMIT:
            raise DailyLimitReached(
                f"Playwright daily limit reached ({DAILY_LIMIT} places). "
                "Resume later, or resume with `-s apify`."
            )
        query = target.query
        max_results = target.max_results

        lat = target.extra.get("tile_lat")
        lng = target.extra.get("tile_lng")
        has_coords = lat is not None and lng is not None
        if has_coords:
            url = MAPS_COORD_URL.format(
                query=quote_plus(query), lat=lat, lng=lng,
                zoom=target.extra.get("tile_zoom", DEFAULT_ZOOM),
            )
        else:
            if target.location:
                query = f"{query} in {target.location}"
            url = MAPS_URL.format(query=quote_plus(query))

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

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(random.randint(2000, 4000))

            await self._dismiss_consent(page)

            try:
                await page.wait_for_selector(SEL_FEED, timeout=10_000)
            except Exception:
                logger.warning("No results feed found for query: %s", query)
                return leads

            place_urls = await self._scroll_and_collect(page, max_results)
            logger.info("Found %d place links", len(place_urls))

            for i, place_url in enumerate(place_urls):
                try:
                    if i > 0:
                        if (i + 1) % 8 == 0:
                            await page.wait_for_timeout(random.randint(7000, 15000))
                        else:
                            await page.wait_for_timeout(random.randint(2000, 5000))
                    if GMapsScraper._daily_count >= DAILY_LIMIT:
                        logger.warning("Daily limit reached mid-scrape (%d). Stopping.", DAILY_LIMIT)
                        break
                    detail = await self._extract_place(page, place_url, context=context, max_reviews=self.reviews_per_place)
                    if detail:
                        lead = self._to_lead(detail, query)
                        leads.append(lead)
                        GMapsScraper._daily_count += 1
                        if (i + 1) % 10 == 0:
                            logger.info("Extracted %d/%d places (daily: %d/%d)", i + 1, len(place_urls), self._daily_count, DAILY_LIMIT)
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
        seen_urls: dict[str, None] = {}
        stale_count = 0
        max_scrolls = target_count * 3 if target_count > 0 else 200

        feed = page.locator(SEL_FEED)

        for _ in range(max_scrolls):
            links = await page.query_selector_all(SEL_PLACE_LINKS)
            for link in links:
                href = await link.get_attribute("href")
                if href and href not in seen_urls:
                    seen_urls[href] = None

            if target_count > 0 and len(seen_urls) >= target_count:
                break

            prev_count = len(seen_urls)
            scroll_count = len(seen_urls)
            await feed.evaluate("el => el.scrollBy(0, el.clientHeight * 0.8)")
            if scroll_count > 0 and scroll_count % 14 == 0:
                await page.wait_for_timeout(random.randint(3200, 7600))
            else:
                await page.wait_for_timeout(random.randint(1200, 3200))

            end_marker = page.locator("text=You've reached the end of the list")
            try:
                if await end_marker.is_visible(timeout=500):
                    break
            except Exception:
                pass

            if len(seen_urls) == prev_count:
                stale_count += 1
                if stale_count >= 5:
                    break
            else:
                stale_count = 0

        return list(seen_urls.keys())

    @staticmethod
    async def _extract_place(page: Page, url: str, context=None, max_reviews: int = 5) -> dict[str, Any] | None:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await page.wait_for_timeout(2000)

        title_el = await page.query_selector(SEL_TITLE)
        if not title_el:
            return None
        title = (await title_el.inner_text()).strip()
        if not title:
            return None

        detail: dict[str, Any] = {"title": title, "url": url}

        rating_el = await page.query_selector(SEL_RATING)
        if rating_el:
            try:
                detail["rating"] = float((await rating_el.inner_text()).strip().replace(",", "."))
            except ValueError:
                pass

        review_el = await page.query_selector(SEL_REVIEW_COUNT)
        if review_el:
            text = await review_el.inner_text()
            match = REVIEW_COUNT_RE.search(text)
            if match:
                detail["review_count"] = int(match.group(1).replace(",", ""))

        cat_el = await page.query_selector(SEL_CATEGORY)
        if cat_el:
            detail["category"] = (await cat_el.inner_text()).strip()

        phone_el = await page.query_selector(SEL_PHONE)
        if phone_el:
            detail["phone"] = (await phone_el.inner_text()).strip()

        addr_el = await page.query_selector(SEL_ADDRESS)
        if addr_el:
            detail["address"] = (await addr_el.inner_text()).strip()

        web_el = await page.query_selector(SEL_WEBSITE)
        if web_el:
            href = await web_el.get_attribute("href")
            if href:
                detail["website"] = href

        price_el = await page.query_selector(SEL_PRICE)
        if price_el:
            label = await price_el.get_attribute("aria-label")
            if label:
                detail["price_level"] = label

        hours_el = await page.query_selector(SEL_HOURS)
        if hours_el:
            label = await hours_el.get_attribute("aria-label")
            if label:
                detail["hours"] = label

        place_match = re.search(r"place/[^/]+/[^/]*!8m2!3d[^!]+!4d[^!]+", url)
        if place_match:
            detail["maps_url"] = url

        if context and max_reviews > 0:
            try:
                from suplex.scraper.review_scraper import scrape_reviews
                review_data = await scrape_reviews(
                    context, page, place_url=url, max_reviews=max_reviews,
                )
                if review_data.get("rating"):
                    detail["rating"] = review_data["rating"]
                if review_data.get("review_count"):
                    detail["review_count"] = review_data["review_count"]
                if review_data.get("reviews"):
                    detail["reviews"] = review_data["reviews"]
            except Exception:
                logger.debug("Review scraping failed for %s", url, exc_info=True)

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
