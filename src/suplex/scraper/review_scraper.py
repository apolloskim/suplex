"""Google Maps review scraper — uses RPC API via browser context.

Ported from suplex-desktop's freeReviewScraper.ts.
Requires a Playwright browser context with Google Maps cookies.
Extracts reviews via the internal listentitiesreviews RPC endpoint.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from typing import Any

from playwright.async_api import BrowserContext, Page

logger = logging.getLogger(__name__)

DATA_ID_RE = re.compile(r"(0x[0-9a-fA-F]{8,}:0x[0-9a-fA-F]{8,})")
RPC_ENDPOINT = "https://www.google.com/maps/preview/review/listentitiesreviews"


def _parse_data_ids(url: str) -> tuple[int, int] | None:
    """Extract FID and CID decimal IDs from a Google Maps URL."""
    match = DATA_ID_RE.search(url)
    if not match:
        return None
    parts = match.group(1).split(":")
    fid_dec = int(parts[0].replace("0x", ""), 16)
    cid_dec = int(parts[1].replace("0x", ""), 16)
    return fid_dec, cid_dec


def _build_pb(fid_dec: int, cid_dec: int, offset: int = 0, limit: int = 10, sort: int = 1) -> str:
    """Build the protobuf-like pb parameter for the RPC endpoint."""
    token = secrets.token_urlsafe(12)
    return (
        f"!1m2!1y{fid_dec}!2y{cid_dec}"
        f"!2m2!1i{offset}!2i{limit}"
        f"!3e{sort}"
        f"!4m5!3b1!4b1!5b1!6b1!7b1"
        f"!5m2!1s{token}!7e81"
    )


def _parse_rpc_response(body: str) -> tuple[list[dict], int | None]:
    """Parse the RPC response into reviews + total count."""
    # Strip XSSI prefix.
    if body.startswith(")]}'"):
        body = body[4:].strip()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return [], None

    # Total review count at payload[5][4].
    total = None
    try:
        if isinstance(payload, list) and len(payload) > 5:
            section = payload[5]
            if isinstance(section, list) and len(section) > 4:
                val = section[4]
                if isinstance(val, (int, float)) and val >= 0:
                    total = int(val)
    except (IndexError, TypeError):
        pass

    # Reviews at payload[2].
    raw_reviews = payload[2] if isinstance(payload, list) and len(payload) > 2 else None
    if not isinstance(raw_reviews, list):
        return [], total

    reviews = []
    for row in raw_reviews:
        if not isinstance(row, list) or len(row) < 5:
            continue

        # row[0] = author info (list), row[1] = relative date, row[3] = text, row[4] = stars
        author = ""
        if isinstance(row[0], list) and len(row[0]) > 1:
            author = str(row[0][1] or "").strip()

        date = str(row[1] or "").strip()
        text = str(row[3] or "").strip()

        stars = None
        if isinstance(row[4], (int, float)) and 1 <= row[4] <= 5:
            stars = int(row[4])

        if not text and not stars:
            continue

        reviews.append({
            "author": author,
            "stars": stars,
            "date": date,
            "text": text[:500],
        })

    return reviews, total


async def scrape_reviews(
    context: BrowserContext,
    page: Page,
    *,
    place_url: str | None = None,
    max_reviews: int = 10,
) -> dict[str, Any]:
    """Scrape Google Maps reviews for the current place page.

    Uses the RPC API with browser context cookies. The page must already
    be on a Google Maps place page.

    Returns: {"rating": float, "review_count": int, "reviews": [...]}
    """
    # Get data IDs from page URL.
    current_url = place_url or page.url
    ids = _parse_data_ids(current_url)

    if not ids:
        # Try extracting from DOM (canonical link, meta tags, anchors).
        ids = await _extract_ids_from_dom(page)

    if not ids:
        logger.debug("No data IDs found for review scraping")
        return {"reviews": [], "review_count": None, "rating": None}

    fid_dec, cid_dec = ids

    # Get rating from the page.
    rating = await page.evaluate("""() => {
        const el = document.querySelector('div.F7nice span[aria-hidden="true"]');
        if (el) return parseFloat(el.textContent.replace(',', '.'));
        const img = document.querySelector('span[role="img"][aria-label*="star"]');
        if (img) {
            const m = img.getAttribute('aria-label').match(/([0-9.]+)/);
            if (m) return parseFloat(m[1]);
        }
        return null;
    }""")

    # Fetch reviews via RPC API.
    all_reviews: list[dict] = []
    total_count = None
    seen_texts: set[str] = set()

    # Try sort=1 (newest) first, then sort=2 (most relevant) for variety.
    for sort_mode in (1, 2):
        if len(all_reviews) >= max_reviews:
            break

        remaining = max_reviews - len(all_reviews)
        limit = min(remaining, 10)  # API max 10 per call.
        pb = _build_pb(fid_dec, cid_dec, offset=0, limit=limit, sort=sort_mode)
        url = f"{RPC_ENDPOINT}?authuser=0&hl=en&gl=us&pb={pb}"

        try:
            resp = await context.request.get(url, headers={"Referer": page.url})
            if resp.status != 200:
                logger.debug("RPC API returned %d for sort=%d", resp.status, sort_mode)
                continue

            body = await resp.text()
            reviews, count = _parse_rpc_response(body)

            if count is not None:
                total_count = count

            for review in reviews:
                # Deduplicate by text prefix.
                key = review["text"][:80].lower()
                if key in seen_texts:
                    continue
                seen_texts.add(key)
                all_reviews.append(review)
                if len(all_reviews) >= max_reviews:
                    break

        except Exception:
            logger.debug("RPC API call failed for sort=%d", sort_mode, exc_info=True)

    logger.info(
        "Reviews: %d extracted (total: %s, rating: %s)",
        len(all_reviews), total_count, rating,
    )

    return {
        "rating": rating,
        "review_count": total_count,
        "reviews": all_reviews,
    }


async def _extract_ids_from_dom(page: Page) -> tuple[int, int] | None:
    """Extract data IDs from DOM elements when URL doesn't have them."""
    raw = await page.evaluate("""() => {
        const re = /(0x[0-9a-fA-F]{8,}:0x[0-9a-fA-F]{8,})/;

        // Try canonical link.
        const canonical = document.querySelector('link[rel="canonical"]');
        if (canonical) {
            const m = (canonical.getAttribute('href') || '').match(re);
            if (m) return m[1];
        }

        // Try meta tags.
        for (const sel of ['meta[property="og:url"]', 'meta[itemprop="url"]', 'meta[name="twitter:url"]']) {
            const el = document.querySelector(sel);
            if (el) {
                const m = (el.getAttribute('content') || '').match(re);
                if (m) return m[1];
            }
        }

        // Try anchors with hex IDs.
        for (const a of document.querySelectorAll('a[href*="0x"]')) {
            const m = (a.getAttribute('href') || '').match(re);
            if (m) return m[1];
        }

        return null;
    }""")

    if raw:
        return _parse_data_ids(raw)
    return None
