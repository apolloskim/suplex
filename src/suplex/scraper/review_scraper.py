from __future__ import annotations

import logging
import re
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page

logger = logging.getLogger(__name__)

REVIEW_CARD = "div[data-review-id][aria-label]"
REVIEWS_TAB = re.compile(r"^Reviews", re.IGNORECASE)
STARS_RE = re.compile(r"(\d)")
MAX_TEXT_LENGTH = 500
MAX_SCROLLS = 10

EXPAND_TRUNCATED_JS = """(limit) => {
    const cards = [...document.querySelectorAll('div[data-review-id][aria-label]')].slice(0, limit);
    for (const card of cards) {
        for (const button of card.querySelectorAll('button[aria-label="See more"]')) button.click();
    }
}"""

EXTRACT_REVIEWS_JS = """(limit) => {
    const seen = new Set();
    const reviews = [];
    for (const card of document.querySelectorAll('div[data-review-id][aria-label]')) {
        const id = card.getAttribute('data-review-id');
        if (seen.has(id)) continue;
        seen.add(id);
        const stars = card.querySelector('[role="img"][aria-label*="star"]');
        const holder = card.querySelector('[data-irrelevant-review-text-id]');
        const body = holder ? document.getElementById(holder.getAttribute('data-irrelevant-review-text-id')) : null;
        reviews.push({
            author: card.getAttribute('aria-label') || '',
            stars: stars ? stars.getAttribute('aria-label') : '',
            date: stars && stars.nextElementSibling ? stars.nextElementSibling.textContent : '',
            text: body ? body.innerText : '',
        });
        if (reviews.length >= limit) break;
    }
    return reviews;
}"""

SCROLL_REVIEWS_JS = """() => {
    const cards = document.querySelectorAll('div[data-review-id][aria-label]');
    if (cards.length) cards[cards.length - 1].scrollIntoView();
    return cards.length;
}"""

_markup_warning_logged = False


async def scrape_reviews(page: Page, *, max_reviews: int, expected_count: int | None = None) -> list[dict[str, Any]]:
    if max_reviews <= 0:
        return []

    try:
        await page.get_by_role("tab", name=REVIEWS_TAB).first.click(timeout=5000)
        await page.wait_for_selector(REVIEW_CARD, timeout=8000)
    except PlaywrightError:
        _warn_if_reviews_expected(expected_count, "the Reviews tab did not open")
        return []

    loaded = 0
    for _ in range(MAX_SCROLLS):
        count = await page.evaluate(SCROLL_REVIEWS_JS)
        if count >= max_reviews or count == loaded:
            break
        loaded = count
        await page.wait_for_timeout(1200)

    await page.evaluate(EXPAND_TRUNCATED_JS, max_reviews)
    await page.wait_for_timeout(400)
    raw_reviews = await page.evaluate(EXTRACT_REVIEWS_JS, max_reviews)

    reviews = [review for review in map(_normalize, raw_reviews) if review["text"] or review["stars"]]
    if not reviews:
        _warn_if_reviews_expected(expected_count, "no review cards could be read")
    logger.info("Reviews: %d extracted", len(reviews))
    return reviews


def _normalize(raw: dict[str, str]) -> dict[str, Any]:
    stars_match = STARS_RE.search(raw.get("stars") or "")
    text = (raw.get("text") or "").strip()
    return {
        "author": (raw.get("author") or "").strip(),
        "stars": int(stars_match.group(1)) if stars_match else None,
        "date": (raw.get("date") or "").strip(),
        "text": text[:MAX_TEXT_LENGTH],
    }


def _warn_if_reviews_expected(expected_count: int | None, reason: str) -> None:
    global _markup_warning_logged
    if not expected_count or _markup_warning_logged:
        return
    _markup_warning_logged = True
    logger.warning(
        "Google Maps lists %d reviews for a place but %s; the page markup may have changed",
        expected_count, reason,
    )
