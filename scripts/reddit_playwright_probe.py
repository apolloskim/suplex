from __future__ import annotations

import argparse
import asyncio
from datetime import date, datetime, timezone
from html import unescape
from html.parser import HTMLParser
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, parse_qsl, quote_plus, urlencode, urlparse, urlunparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


MAC_BROWSER_EXECUTABLES = {
    "com.google.chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "com.google.Chrome.canary": "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    "com.microsoft.edgemac": "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "company.thebrowser.Browser": "/Applications/Arc.app/Contents/MacOS/Arc",
    "com.brave.Browser": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
}

REDDIT_COMMENTS_PATH_RE = re.compile(r"^/r/[^/]+/comments/[^/]+/")
RE_AGE = re.compile(r"\b(\d+\s*(?:m|h|d|mo|y)\s+ago)\b", re.IGNORECASE)
RE_SCORE = re.compile(r"\b(\d+)\s+votes?\b", re.IGNORECASE)
RE_COMMENTS = re.compile(r"\b(\d+)\s+comments?\b", re.IGNORECASE)
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find Reddit posts via Playwright and extract structured post/comment data.",
    )
    parser.add_argument(
        "--engine",
        choices=["duckduckgo", "reddit"],
        default="duckduckgo",
        help="Search surface to use before visiting Reddit posts.",
    )
    parser.add_argument("--query", required=True, help="Lead-intent query to run")
    parser.add_argument(
        "--subreddit",
        required=True,
        help="Subreddit name without r/. This tool currently targets one subreddit at a time.",
    )
    parser.add_argument("--limit", type=int, default=5, help="How many posts to visit")
    parser.add_argument(
        "--start-date",
        help="Inclusive post date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--end-date",
        help="Inclusive post date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--comments-limit",
        type=int,
        default=5,
        help="How many comments to keep in output per post. Use 0 for all collected comments.",
    )
    parser.add_argument(
        "--comment-start-date",
        help="Inclusive comment date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--comment-end-date",
        help="Inclusive comment date filter in YYYY-MM-DD.",
    )
    parser.add_argument(
        "--comment-sort",
        choices=["best", "new", "top", "old", "controversial", "qa"],
        default="best",
        help="Comment order to request on Reddit post pages.",
    )
    parser.add_argument(
        "--expand-replies",
        action="store_true",
        help="Follow Reddit's 'More replies' permalinks recursively to collect more of the thread.",
    )
    parser.add_argument(
        "--reply-pages-limit",
        type=int,
        default=20,
        help="Max reply permalink pages to visit when --expand-replies is enabled.",
    )
    parser.add_argument(
        "--time",
        choices=["hour", "day", "week", "month", "year", "all"],
        default="month",
        help="Time window for Reddit-native search. Included in the web search query too.",
    )
    parser.add_argument(
        "--browser-executable",
        help="Path to the browser executable. If omitted, tries the default macOS browser.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Open a visible browser window instead of running headless.",
    )
    parser.add_argument(
        "--output",
        help="Optional JSON output path. If omitted, prints to stdout.",
    )
    parser.add_argument(
        "--wait-ms",
        type=int,
        default=1200,
        help="Pause between page visits. Useful for gentler browsing.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Include extra raw search-card text and raw comment text.",
    )
    return parser.parse_args()


def detect_default_browser_executable() -> str | None:
    try:
        result = subprocess.run(
            ["defaults", "read", "com.apple.LaunchServices/com.apple.launchservices.secure", "LSHandlers"],
            capture_output=True,
            text=True,
            check=True,
        )
    except Exception:
        return None

    text = result.stdout
    marker = 'LSHandlerContentType = "com.apple.default-app.web-browser";'
    marker_index = text.find(marker)
    if marker_index == -1:
        return None

    following = text[marker_index: marker_index + 400]
    match = re.search(r'LSHandlerRoleAll = "([^"]+)";', following)
    if not match:
        return None

    bundle_id = match.group(1)
    path = MAC_BROWSER_EXECUTABLES.get(bundle_id)
    if path and Path(path).exists():
        return path
    return None


def pick_browser_executable(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    detected = detect_default_browser_executable()
    if detected:
        return detected
    chrome = MAC_BROWSER_EXECUTABLES["com.google.chrome"]
    return chrome if Path(chrome).exists() else None


def build_reddit_search_url(subreddit: str, query: str, time_window: str) -> str:
    return (
        f"https://www.reddit.com/r/{subreddit}/search/"
        f"?q={quote_plus(query)}&restrict_sr=1&sort=new&t={time_window}"
    )


def build_duckduckgo_search_url(subreddit: str, query: str, time_window: str) -> str:
    del time_window  # DDG date controls are UI-driven, so we keep the query broad here.
    search = f'site:reddit.com/r/{subreddit}/comments "{query}"'
    return f"https://duckduckgo.com/?q={quote_plus(search)}&ia=web"


def extract_scalar(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None


def strip_query_and_fragment(url: str) -> str:
    parsed = urlparse(url)
    cleaned = ParseResult(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=parsed.path.rstrip("/") + "/",
        params="",
        query="",
        fragment="",
    )
    return urlunparse(cleaned)


def reddit_absolute_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme and parsed.netloc:
        return value
    if value.startswith("/"):
        return f"https://www.reddit.com{value}"
    return value


def with_query_param(url: str, key: str, value: str | None) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if value is None:
        query.pop(key, None)
    else:
        query[key] = value
    rebuilt = ParseResult(
        scheme=parsed.scheme,
        netloc=parsed.netloc,
        path=parsed.path,
        params=parsed.params,
        query=urlencode(query),
        fragment="",
    )
    return urlunparse(rebuilt)


def is_reddit_post_url(url: str, subreddit: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc not in {"www.reddit.com", "reddit.com", "old.reddit.com"}:
        return False
    if not REDDIT_COMMENTS_PATH_RE.match(parsed.path):
        return False
    return f"/r/{subreddit}/comments/" in parsed.path


def dedupe_search_results(
    results: list[dict[str, Any]],
    subreddit: str,
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = result.get("url")
        if not url or not is_reddit_post_url(url, subreddit):
            continue
        canonical = strip_query_and_fragment(url)
        if canonical in seen_urls:
            continue
        seen_urls.add(canonical)
        result["url"] = canonical
        deduped.append(result)
    return deduped


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def matches_date_window(
    timestamp: str | None,
    start_date: date | None,
    end_date: date | None,
) -> bool:
    if not start_date and not end_date:
        return True
    parsed = parse_timestamp(timestamp)
    if not parsed:
        return False
    day = parsed.date()
    if start_date and day < start_date:
        return False
    if end_date and day > end_date:
        return False
    return True


def extract_card_text(body_lines: list[str], title: str, start_index: int) -> tuple[str, int]:
    for index in range(start_index, len(body_lines)):
        if body_lines[index] != title:
            continue

        block = [body_lines[index]]
        for next_index in range(index + 1, min(index + 10, len(body_lines))):
            next_line = body_lines[next_index]
            if next_line.startswith("r/") and len(block) > 1:
                break
            block.append(next_line)
            if "comment" in next_line.lower():
                return "\n".join(block), next_index + 1
        return "\n".join(block), index + 1

    return "", start_index


async def launch_browser(executable_path: str | None, headful: bool) -> tuple[Browser, BrowserContext]:
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        executable_path=executable_path,
        headless=not headful,
        args=["--disable-blink-features=AutomationControlled"],
    )
    browser._suplex_playwright = pw  # type: ignore[attr-defined]
    context = await browser.new_context(
        viewport={"width": 1440, "height": 900},
        locale="en-US",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/135.0.0.0 Safari/537.36"
        ),
    )
    return browser, context


async def close_browser(browser: Browser, context: BrowserContext) -> None:
    await context.close()
    await browser.close()
    pw = getattr(browser, "_suplex_playwright", None)
    if pw is not None:
        await pw.stop()


async def load_reddit_page(
    page: Page,
    url: str,
    comment_sort: str,
) -> None:
    visit_url = with_query_param(url, "sort", comment_sort)
    await page.goto(visit_url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_000)


async def search_reddit(
    page: Page,
    subreddit: str,
    query: str,
    time_window: str,
) -> tuple[str, list[dict[str, Any]]]:
    url = build_reddit_search_url(subreddit, query, time_window)
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_000)

    payload: dict[str, Any] = await page.evaluate(
        """() => {
            const trackers = Array.from(document.querySelectorAll("search-telemetry-tracker"));
            return {
                title: document.title,
                final_url: location.href,
                body_text: document.body.innerText,
                blocked: document.title.includes("Prove your humanity"),
                trackers: trackers.map((tracker) => {
                    const link = tracker.querySelector('a[href*="/comments/"]');
                    const href = link ? new URL(link.getAttribute("href"), window.location.origin).href : null;
                    const title =
                        link?.getAttribute("aria-label") ||
                        link?.textContent?.trim() ||
                        null;
                    let context = {};
                    try {
                        context = JSON.parse(tracker.getAttribute("data-faceplate-tracking-context") || "{}");
                    } catch (err) {
                        context = {};
                    }
                    return { title, href, context };
                }),
            };
        }""",
    )

    if payload["blocked"]:
        return payload["final_url"], []

    body_lines = [line.strip() for line in payload["body_text"].splitlines() if line.strip()]
    results: list[dict[str, Any]] = []
    cursor = 0
    for tracker in payload["trackers"]:
        href = tracker.get("href")
        title = tracker.get("title")
        if not href or not title:
            continue
        card_text, cursor = extract_card_text(body_lines, title, cursor)
        context = tracker.get("context", {})
        results.append(
            {
                "found_via": "reddit",
                "search_title": title,
                "url": href,
                "subreddit": context.get("subreddit", {}).get("name") or subreddit,
                "position": context.get("action_info", {}).get("position"),
                "age_text": extract_scalar(RE_AGE, card_text),
                "score_text": extract_scalar(RE_SCORE, card_text),
                "comments_text": extract_scalar(RE_COMMENTS, card_text),
                "raw_search_text": card_text,
            }
        )
    return payload["final_url"], dedupe_search_results(results, subreddit)


async def search_duckduckgo(
    page: Page,
    subreddit: str,
    query: str,
    time_window: str,
) -> tuple[str, list[dict[str, Any]]]:
    url = build_duckduckgo_search_url(subreddit, query, time_window)
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_000)

    results: list[dict[str, Any]] = await page.evaluate(
        """() => {
            const anchors = Array.from(
                document.querySelectorAll('a[href*="reddit.com/r/"][href*="/comments/"]'),
            );
            return anchors.map((anchor, index) => {
                const href = anchor.href;
                const text = (anchor.textContent || "").trim();
                const root = anchor.closest("article") || anchor.closest('[data-layout="organic"]') || anchor;
                const raw = (root.innerText || text).trim();
                return {
                    found_via: "duckduckgo",
                    search_title: text || null,
                    url: href,
                    position: index,
                    age_text: extractAge(raw),
                    score_text: null,
                    comments_text: null,
                    raw_search_text: raw,
                };
            });

            function extractAge(text) {
                const match = text.match(/Posted\\s+([^\\n]+)/i);
                return match ? match[1].trim() : null;
            }
        }""",
    )

    return page.url, dedupe_search_results(results, subreddit)


def sanitize_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def compact_text(text: str) -> str:
    lines = sanitize_lines(text)
    filtered = [
        line for line in lines
        if line not in {"Reply", "Share", "Award", "Report", "Save", "Follow"}
    ]
    return "\n".join(filtered)


class AnchorHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a" or self._href is not None:
            return
        attr_map = dict(attrs)
        href = attr_map.get("href")
        if not href:
            return
        self._href = href
        self._parts = []

    def handle_data(self, data: str) -> None:
        if self._href is None:
            return
        self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or self._href is None:
            return
        self.links.append(
            {
                "text": "".join(self._parts).strip(),
                "href": self._href,
            },
        )
        self._href = None
        self._parts = []


def extract_links_from_html(html_text: str) -> list[dict[str, str]]:
    if not html_text:
        return []
    parser = AnchorHTMLParser()
    parser.feed(html_text)
    parser.close()
    return parser.links


def normalize_thing_id(value: str | None, prefix: str) -> str | None:
    if not value:
        return None
    return value if value.startswith(prefix) else f"{prefix}{value}"


def normalize_comment_id(value: str | None) -> str | None:
    return normalize_thing_id(value, "t1_")


def normalize_post_id(value: str | None) -> str | None:
    return normalize_thing_id(value, "t3_")


def isoformat_utc_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+0000")


def string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def is_missing(value: Any) -> bool:
    return (
        value is None
        or value == ""
        or value == ()
        or value == []
        or value == {}
    )


def dedupe_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


def build_reddit_comments_json_url(
    url: str,
    comment_sort: str,
    limit: int = 500,
    depth: int = 10,
) -> str:
    canonical = strip_query_and_fragment(url)
    parsed = urlparse(canonical)
    path = parsed.path.rstrip("/") + "/.json"
    query = urlencode(
        {
            "sort": comment_sort,
            "limit": str(limit),
            "depth": str(depth),
            "raw_json": "1",
        },
    )
    return urlunparse(
        ParseResult(
            scheme=parsed.scheme or "https",
            netloc=parsed.netloc or "www.reddit.com",
            path=path,
            params="",
            query=query,
            fragment="",
        ),
    )


def build_reddit_morechildren_url(
    post_id: str,
    child_ids: list[str],
    comment_sort: str,
) -> str:
    clean_child_ids = [
        child.removeprefix("t1_")
        for child in child_ids
        if child
    ]
    query = urlencode(
        {
            "api_type": "json",
            "link_id": post_id,
            "children": ",".join(clean_child_ids),
            "sort": comment_sort,
            "limit_children": "false",
            "raw_json": "1",
        },
    )
    return f"https://www.reddit.com/api/morechildren.json?{query}"


async def fetch_browser_response(
    page: Page,
    url: str,
    method: str = "GET",
    body: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return await page.evaluate(
        """async ({ url, method, body, headers }) => {
            const response = await fetch(url, {
                method,
                credentials: "include",
                headers: headers || {},
                body,
            });
            return {
                ok: response.ok,
                status: response.status,
                content_type: response.headers.get("content-type"),
                text: await response.text(),
            };
        }""",
        {
            "url": url,
            "method": method,
            "body": body,
            "headers": headers,
        },
    )


def walk_comment_listing_nodes(
    nodes: list[dict[str, Any]],
    raw_comments: list[dict[str, Any]],
    more_nodes: list[dict[str, Any]],
    default_depth: int = 0,
) -> None:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("kind")
        data = node.get("data") or {}
        if kind == "t1":
            comment_id = normalize_comment_id(data.get("name") or data.get("id"))
            if not comment_id:
                continue
            depth_value = data.get("depth")
            depth = int(depth_value) if isinstance(depth_value, int | float) else default_depth
            replies = data.get("replies")
            reply_children = (
                replies.get("data", {}).get("children", [])
                if isinstance(replies, dict)
                else []
            )
            raw_comments.append(
                {
                    "comment_id": comment_id,
                    "parent_id": data.get("parent_id"),
                    "depth": depth,
                    "data": data,
                },
            )
            walk_comment_listing_nodes(reply_children, raw_comments, more_nodes, depth + 1)
            continue

        if kind == "more":
            more_nodes.append(
                {
                    "id": data.get("name") or data.get("id"),
                    "parent_id": data.get("parent_id"),
                    "depth": data.get("depth", default_depth),
                    "count": data.get("count"),
                    "children": list(data.get("children") or []),
                },
            )


def parse_reddit_comments_listing(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    children = []
    if isinstance(payload, list) and len(payload) > 1:
        children = payload[1].get("data", {}).get("children", [])
    raw_comments: list[dict[str, Any]] = []
    more_nodes: list[dict[str, Any]] = []
    walk_comment_listing_nodes(children, raw_comments, more_nodes)
    return {
        "raw_comments": raw_comments,
        "more_nodes": more_nodes,
    }


def parse_reddit_morechildren_listing(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    things = payload.get("json", {}).get("data", {}).get("things", [])
    raw_comments: list[dict[str, Any]] = []
    more_nodes: list[dict[str, Any]] = []
    walk_comment_listing_nodes(things, raw_comments, more_nodes)
    return {
        "raw_comments": raw_comments,
        "more_nodes": more_nodes,
    }


async def fetch_reddit_comments_json_payload(
    page: Page,
    url: str,
    comment_sort: str,
) -> dict[str, Any]:
    json_url = build_reddit_comments_json_url(url, comment_sort)
    response = await fetch_browser_response(page, json_url)
    result = {
        "json_url": json_url,
        **response,
        "raw_comments": [],
        "more_nodes": [],
    }
    if not response.get("ok"):
        return result
    try:
        parsed = parse_reddit_comments_listing(response["text"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
        return result
    result.update(parsed)
    return result


async def fetch_reddit_morechildren_payload(
    page: Page,
    post_id: str,
    child_ids: list[str],
    comment_sort: str,
) -> dict[str, Any]:
    morechildren_url = build_reddit_morechildren_url(post_id, child_ids, comment_sort)
    response = await fetch_browser_response(page, morechildren_url)
    result = {
        "url": morechildren_url,
        **response,
        "raw_comments": [],
        "more_nodes": [],
    }
    if not response.get("ok"):
        return result
    try:
        parsed = parse_reddit_morechildren_listing(response["text"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
        return result
    result.update(parsed)
    return result


async def capture_post_payload(
    page: Page,
    url: str,
    comment_sort: str,
) -> dict[str, Any]:
    await load_reddit_page(page, url, comment_sort)

    return await page.evaluate(
        """() => {
            const title = document.title;
            const blocked = title.includes("Prove your humanity");
            const post = document.querySelector("shreddit-post");
            const body = post?.querySelector("shreddit-post-text-body");
            const comments = Array.from(document.querySelectorAll("shreddit-comment"));
            const relatedLinks = Array.from(document.querySelectorAll('a[href*="/comments/"]'))
                .map((anchor) => ({
                    text: (anchor.textContent || "").trim(),
                    href: anchor.href,
                }))
                .filter((item) => item.href !== location.href);
            const moreReplyLinks = Array.from(
                document.querySelectorAll('a[slot="more-comments-permalink"]'),
            ).map((anchor) => anchor.href);
            const moreReplyPartials = Array.from(
                document.querySelectorAll('faceplate-partial[method="post"][src*="/svc/shreddit/more-comments/"]'),
            ).map((partial) => ({
                src: new URL(partial.getAttribute("src"), location.origin).href,
                cursor: partial.querySelector('input[name="cursor"]')?.value || null,
                parent_id: findParentThingId(partial),
            }));

            function extractHiddenBodyText(bodyNode) {
                if (!bodyNode) {
                    return "";
                }
                return Array.from(bodyNode.querySelectorAll("span.hidden, [hidden]"))
                    .map((node) => (node.textContent || "").trim())
                    .filter(Boolean)
                    .join("\\n");
            }

            function pickBodyText(visibleBodyText, hiddenBodyText, fullBodyText) {
                if ((visibleBodyText || "").trim()) {
                    return { text: visibleBodyText, source: "visible" };
                }
                if ((hiddenBodyText || "").trim()) {
                    return { text: hiddenBodyText, source: "hidden" };
                }
                if ((fullBodyText || "").trim()) {
                    return { text: fullBodyText, source: "text_content" };
                }
                return { text: "", source: "empty" };
            }

            return {
                blocked,
                title,
                final_url: location.href,
                post: post ? {
                    attrs: Object.fromEntries(Array.from(post.attributes).map((attr) => [attr.name, attr.value])),
                    header_text: post.querySelector('[slot="credit-bar"]')?.innerText || "",
                    body_text: body?.innerText || "",
                    full_text: post.innerText || "",
                } : null,
                times: Array.from(document.querySelectorAll("time")).slice(0, 20).map((node) => ({
                    text: (node.textContent || "").trim(),
                    datetime: node.getAttribute("datetime"),
                })),
                comments_reported: post?.getAttribute("comment-count") || null,
                visible_comment_count: comments.length,
                comments: comments.map((comment) => {
                    const metaNode = Array.from(comment.children)
                        .find((child) => child.getAttribute?.("slot") === "commentMeta");
                    const bodyNode = Array.from(comment.children)
                        .find((child) => child.getAttribute?.("slot") === "comment");
                    const visibleBodyText = bodyNode?.innerText || "";
                    const hiddenBodyText = extractHiddenBodyText(bodyNode);
                    const fullBodyText = bodyNode?.textContent || "";
                    const pickedBody = pickBodyText(visibleBodyText, hiddenBodyText, fullBodyText);
                    const authorLink = metaNode?.querySelector('a[href^="/user/"]');
                    const badgesNode = comment.querySelector("shreddit-comment-badges");
                    const authorModifierNode = comment.querySelector("shreddit-comment-author-modifier-icon");
                    return {
                        attrs: Object.fromEntries(Array.from(comment.attributes).map((attr) => [attr.name, attr.value])),
                        badge_attrs: badgesNode
                            ? Object.fromEntries(Array.from(badgesNode.attributes).map((attr) => [attr.name, attr.value]))
                            : {},
                        author_modifier_attrs: authorModifierNode
                            ? Object.fromEntries(Array.from(authorModifierNode.attributes).map((attr) => [attr.name, attr.value]))
                            : {},
                        author_url: authorLink ? new URL(authorLink.getAttribute("href"), location.origin).href : null,
                        text: comment.innerText || "",
                        meta_text: metaNode?.innerText || "",
                        visible_body_text: visibleBodyText,
                        hidden_body_text: hiddenBodyText,
                        full_body_text: fullBodyText,
                        body_text: pickedBody.text,
                        body_text_source: pickedBody.source,
                        body_html: bodyNode?.outerHTML || "",
                        body_links: bodyNode
                            ? Array.from(bodyNode.querySelectorAll("a[href]"))
                                .filter((anchor) => {
                                    const rawHref = anchor.getAttribute("href") || "";
                                    return rawHref && rawHref !== "#";
                                })
                                .map((anchor) => ({
                                    text: (anchor.textContent || "").trim(),
                                    href: anchor.href,
                                }))
                            : [],
                        parent_id: findParentThingId(comment),
                        direct_reply_ids: Array.from(comment.children)
                            .filter((child) => child.tagName === "SHREDDIT-COMMENT")
                            .map((child) => child.getAttribute("thingid"))
                            .filter(Boolean),
                        more_reply_links: Array.from(comment.children)
                            .filter((child) => child.tagName === "A" && child.getAttribute("slot") === "more-comments-permalink")
                            .map((child) => child.href),
                    };
                }),
                related_links: relatedLinks.slice(0, 40),
                more_reply_links: moreReplyLinks,
                more_reply_partials: moreReplyPartials,
            };

            function findParentThingId(node) {
                let current = node.parentElement;
                while (current) {
                    if (current.tagName === "SHREDDIT-COMMENT") {
                        return current.getAttribute("thingid");
                    }
                    current = current.parentElement;
                }
                return null;
            }
        }""",
    )


async def fetch_more_comments_payload(
    page: Page,
    partial_src: str,
    cursor: str,
) -> dict[str, Any]:
    return await page.evaluate(
        """async ({ partialSrc, cursor }) => {
            function getCookie(name) {
                const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
                return match ? decodeURIComponent(match[1]) : null;
            }

            function extractHiddenBodyText(bodyNode) {
                if (!bodyNode) {
                    return "";
                }
                return Array.from(bodyNode.querySelectorAll("span.hidden, [hidden]"))
                    .map((node) => (node.textContent || "").trim())
                    .filter(Boolean)
                    .join("\\n");
            }

            function pickBodyText(visibleBodyText, hiddenBodyText, fullBodyText) {
                if ((visibleBodyText || "").trim()) {
                    return { text: visibleBodyText, source: "visible" };
                }
                if ((hiddenBodyText || "").trim()) {
                    return { text: hiddenBodyText, source: "hidden" };
                }
                if ((fullBodyText || "").trim()) {
                    return { text: fullBodyText, source: "text_content" };
                }
                return { text: "", source: "empty" };
            }

            function findParentThingId(node) {
                let current = node.parentElement;
                while (current) {
                    if (current.tagName === "SHREDDIT-COMMENT") {
                        return current.getAttribute("thingid");
                    }
                    current = current.parentElement;
                }
                return null;
            }

            function extractComment(comment) {
                const metaNode = Array.from(comment.children)
                    .find((child) => child.getAttribute?.("slot") === "commentMeta");
                const bodyNode = Array.from(comment.children)
                    .find((child) => child.getAttribute?.("slot") === "comment");
                const visibleBodyText = bodyNode?.innerText || "";
                const hiddenBodyText = extractHiddenBodyText(bodyNode);
                const fullBodyText = bodyNode?.textContent || "";
                const pickedBody = pickBodyText(visibleBodyText, hiddenBodyText, fullBodyText);
                const authorLink = metaNode?.querySelector('a[href^="/user/"]');
                const badgesNode = comment.querySelector("shreddit-comment-badges");
                const authorModifierNode = comment.querySelector("shreddit-comment-author-modifier-icon");
                return {
                    attrs: Object.fromEntries(Array.from(comment.attributes).map((attr) => [attr.name, attr.value])),
                    badge_attrs: badgesNode
                        ? Object.fromEntries(Array.from(badgesNode.attributes).map((attr) => [attr.name, attr.value]))
                        : {},
                    author_modifier_attrs: authorModifierNode
                        ? Object.fromEntries(Array.from(authorModifierNode.attributes).map((attr) => [attr.name, attr.value]))
                        : {},
                    author_url: authorLink ? new URL(authorLink.getAttribute("href"), location.origin).href : null,
                    text: comment.innerText || "",
                    meta_text: metaNode?.innerText || "",
                    visible_body_text: visibleBodyText,
                    hidden_body_text: hiddenBodyText,
                    full_body_text: fullBodyText,
                    body_text: pickedBody.text,
                    body_text_source: pickedBody.source,
                    body_html: bodyNode?.outerHTML || "",
                    body_links: bodyNode
                        ? Array.from(bodyNode.querySelectorAll("a[href]"))
                            .filter((anchor) => {
                                const rawHref = anchor.getAttribute("href") || "";
                                return rawHref && rawHref !== "#";
                            })
                            .map((anchor) => ({
                                text: (anchor.textContent || "").trim(),
                                href: anchor.href,
                            }))
                        : [],
                    parent_id: comment.getAttribute("parentid") || findParentThingId(comment),
                    direct_reply_ids: Array.from(comment.children)
                        .filter((child) => child.tagName === "SHREDDIT-COMMENT")
                        .map((child) => child.getAttribute("thingid"))
                        .filter(Boolean),
                    more_reply_links: Array.from(comment.children)
                        .filter((child) => child.tagName === "A" && child.getAttribute("slot") === "more-comments-permalink")
                        .map((child) => child.href),
                };
            }

            function extractMoreReplyPartial(partial) {
                return {
                    src: new URL(partial.getAttribute("src"), location.origin).href,
                    cursor: partial.querySelector('input[name="cursor"]')?.value || null,
                    parent_id: findParentThingId(partial),
                };
            }

            const csrfToken = getCookie("csrf_token");
            const body = new URLSearchParams();
            body.set("cursor", cursor);
            if (csrfToken) {
                body.set("csrf_token", csrfToken);
            }

            const response = await fetch(partialSrc, {
                method: "POST",
                credentials: "include",
                headers: {
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                body,
            });
            const text = await response.text();
            const template = document.createElement("template");
            template.innerHTML = text;
            const root = template.content;
            const comments = Array.from(root.querySelectorAll("shreddit-comment"));
            const moreReplyPartials = Array.from(
                root.querySelectorAll('faceplate-partial[method="post"][src*="/svc/shreddit/more-comments/"]'),
            ).map(extractMoreReplyPartial);

            return {
                ok: response.ok,
                status: response.status,
                content_type: response.headers.get("content-type"),
                comments: comments.map(extractComment),
                more_reply_partials: moreReplyPartials,
            };
        }""",
        {"partialSrc": partial_src, "cursor": cursor},
    )


def comment_from_json_item(comment: dict[str, Any], debug: bool = False) -> dict[str, Any]:
    data = comment["data"]
    comment_id = comment["comment_id"]
    raw_parent_id = comment.get("parent_id") or data.get("parent_id")
    if isinstance(raw_parent_id, str) and raw_parent_id.startswith("t3_"):
        parent_id = None
    else:
        parent_id = normalize_comment_id(raw_parent_id)
    raw_post_id = data.get("link_id")
    if not raw_post_id and isinstance(raw_parent_id, str) and raw_parent_id.startswith("t3_"):
        raw_post_id = raw_parent_id
    body_text = compact_text(data.get("body") or "")
    body_html = (unescape(data.get("body_html") or "").replace('\\"', '"')) or None
    distinguished = data.get("distinguished")
    author = data.get("author")
    author_url = (
        reddit_absolute_url(f"/user/{author}/")
        if author and author != "[deleted]"
        else None
    )
    body_links = [
        {
            "text": compact_text(link.get("text", "")),
            "url": strip_query_and_fragment(link["href"]),
        }
        for link in extract_links_from_html(body_html or "")
        if link.get("href")
    ]
    item = {
        "id": comment_id,
        "comment_id": comment_id,
        "short_id": comment_id.removeprefix("t1_") if comment_id else None,
        "author": author,
        "author_url": author_url,
        "author_label": f"Comment from {author}" if author else None,
        "created": isoformat_utc_timestamp(data.get("created_utc")),
        "permalink": data.get("permalink"),
        "url": reddit_absolute_url(data.get("permalink")),
        "depth": string_or_none(comment.get("depth")),
        "parent_id": parent_id,
        "parent_comment_id": parent_id,
        "is_top_level": parent_id is None,
        "score": string_or_none(data.get("score")),
        "award_count": string_or_none(data.get("total_awards_received")),
        "content_type": data.get("comment_type") or "text",
        "post_id": normalize_post_id(raw_post_id),
        "reload_url": None,
        "reply_permalink": None,
        "is_collapsed": bool(data.get("collapsed")),
        "distinguished_as": distinguished.upper() if isinstance(distinguished, str) else None,
        "is_stickied": bool(data.get("stickied")),
        "is_locked": bool(data.get("locked")),
        "meta_text": "",
        "raw_text": body_text,
        "body_text": body_text,
        "body_text_source": "json",
        "thread_text": body_text,
        "body_html": body_html,
        "body_links": body_links,
        "direct_reply_ids": [],
        "direct_reply_count": 0,
        "more_reply_links": [],
    }
    if debug:
        item["debug"] = {
            "json": data,
        }
    return item


def comment_from_payload_item(comment: dict[str, Any], debug: bool = False) -> dict[str, Any]:
    comment_text = comment["body_text"] or comment["text"]
    comment_id = comment["attrs"].get("thingid")
    parent_id = comment.get("parent_id")
    badge_attrs = comment.get("badge_attrs", {})
    modifier_attrs = comment.get("author_modifier_attrs", {})
    item = {
        "id": comment_id,
        "comment_id": comment_id,
        "short_id": comment_id.removeprefix("t1_") if comment_id else None,
        "author": comment["attrs"].get("author"),
        "author_url": comment.get("author_url"),
        "author_label": comment["attrs"].get("arialabel"),
        "created": comment["attrs"].get("created"),
        "permalink": comment["attrs"].get("permalink"),
        "url": reddit_absolute_url(comment["attrs"].get("permalink")),
        "depth": comment["attrs"].get("depth"),
        "parent_id": parent_id,
        "parent_comment_id": parent_id,
        "is_top_level": parent_id is None,
        "score": comment["attrs"].get("score"),
        "award_count": comment["attrs"].get("award-count"),
        "content_type": comment["attrs"].get("content-type"),
        "post_id": comment["attrs"].get("postid"),
        "reload_url": reddit_absolute_url(comment["attrs"].get("reload-url")),
        "reply_permalink": reddit_absolute_url(comment["attrs"].get("reply-permalink")),
        "is_collapsed": "collapsed" in comment["attrs"],
        "distinguished_as": badge_attrs.get("distinguished-as") or modifier_attrs.get("distinguished-as"),
        "is_stickied": "stickied" in badge_attrs,
        "is_locked": "locked" in badge_attrs,
        "meta_text": compact_text(comment.get("meta_text", "")),
        "raw_text": compact_text(comment["text"]),
        "body_text": compact_text(comment_text),
        "body_text_source": comment.get("body_text_source"),
        "thread_text": compact_text(comment["text"]),
        "body_html": comment.get("body_html"),
        "body_links": [
            {
                "text": compact_text(item.get("text", "")),
                "url": strip_query_and_fragment(item["href"]),
            }
            for item in comment.get("body_links", [])
            if item.get("href")
        ],
        "direct_reply_ids": comment.get("direct_reply_ids", []),
        "direct_reply_count": len(comment.get("direct_reply_ids", [])),
        "more_reply_links": [
            strip_query_and_fragment(url)
            for url in comment.get("more_reply_links", [])
        ],
    }
    if debug:
        item["debug"] = {
            "visible_body_text": comment.get("visible_body_text", ""),
            "hidden_body_text": comment.get("hidden_body_text", ""),
            "full_body_text": comment.get("full_body_text", ""),
            "attrs": comment.get("attrs", {}),
            "badge_attrs": badge_attrs,
            "author_modifier_attrs": modifier_attrs,
        }
    return item


def merge_comment_records(existing: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in new.items():
        if key == "debug":
            debug_payload = dict(merged.get("debug", {}))
            debug_payload.update(value or {})
            merged["debug"] = debug_payload
            continue
        if isinstance(value, list):
            merged[key] = dedupe_preserve([*(merged.get(key) or []), *value])
            continue
        if key in {"is_collapsed", "is_stickied", "is_locked", "is_top_level"}:
            merged[key] = bool(merged.get(key)) or bool(value)
            continue
        if is_missing(merged.get(key)) and not is_missing(value):
            merged[key] = value
    return merged


async def extract_post(
    context: BrowserContext,
    url: str,
    comments_limit: int,
    comment_sort: str,
    expand_replies: bool,
    reply_pages_limit: int,
    comment_start_date: date | None,
    comment_end_date: date | None,
    debug: bool,
) -> dict[str, Any]:
    page = await context.new_page()
    try:
        payload = await capture_post_payload(page, url, comment_sort)
        if payload["blocked"] or not payload["post"]:
            return {
                "url": strip_query_and_fragment(url),
                "blocked": True,
                "title": payload["title"],
            }

        post_attrs = payload["post"]["attrs"]
        post_text = payload["post"]["body_text"] or payload["post"]["full_text"]
        comments_by_id: dict[str, dict[str, Any]] = {}
        comment_order: list[str] = []
        reported_comment_count = None
        try:
            reported_comment_count = int(post_attrs.get("comment-count") or "")
        except ValueError:
            reported_comment_count = None

        def _add_comment(comment: dict[str, Any]) -> None:
            comment_id = comment["id"]
            if not comment_id:
                return
            if comment_id not in comments_by_id:
                comment_order.append(comment_id)
                comments_by_id[comment_id] = comment
                return
            comments_by_id[comment_id] = merge_comment_records(comments_by_id[comment_id], comment)

        json_payload = await fetch_reddit_comments_json_payload(page, payload["final_url"], comment_sort)
        json_fetch_used = bool(json_payload.get("ok") and json_payload.get("raw_comments"))
        json_parse_error = json_payload.get("parse_error")
        json_more_nodes_initial = len(json_payload.get("more_nodes", []))
        if json_fetch_used:
            for comment_item in json_payload.get("raw_comments", []):
                _add_comment(comment_from_json_item(comment_item, debug=debug))

        morechildren_requests = 0
        pending_morechildren_nodes: list[dict[str, Any]] = list(json_payload.get("more_nodes", []))
        seen_morechildren_keys: set[str] = set()
        if expand_replies and json_fetch_used:
            while pending_morechildren_nodes and morechildren_requests < reply_pages_limit:
                node = pending_morechildren_nodes.pop(0)
                child_ids = dedupe_preserve(
                    [
                        normalize_comment_id(child) or child
                        for child in node.get("children", [])
                    ],
                )
                if not child_ids:
                    continue
                morechildren_key = ",".join(sorted(child_ids))
                if morechildren_key in seen_morechildren_keys:
                    continue
                seen_morechildren_keys.add(morechildren_key)
                morechildren_payload = await fetch_reddit_morechildren_payload(
                    page,
                    normalize_post_id(post_attrs.get("id")) or "",
                    child_ids,
                    comment_sort,
                )
                if not morechildren_payload.get("ok"):
                    continue
                for comment_item in morechildren_payload.get("raw_comments", []):
                    _add_comment(comment_from_json_item(comment_item, debug=debug))
                pending_morechildren_nodes.extend(morechildren_payload.get("more_nodes", []))
                morechildren_requests += 1

        if not json_fetch_used:
            for comment_item in payload["comments"]:
                _add_comment(comment_from_payload_item(comment_item, debug=debug))

        visited_reply_urls: set[str] = set()
        queued_reply_urls: set[str] = set()
        reply_queue: list[str] = []
        visited_partial_keys: set[str] = set()
        queued_partial_keys: set[str] = set()
        partial_queue: list[dict[str, str]] = []

        def _enqueue(urls: list[str]) -> None:
            for reply_url in urls:
                canonical = strip_query_and_fragment(reply_url)
                if canonical in visited_reply_urls or canonical in queued_reply_urls:
                    continue
                queued_reply_urls.add(canonical)
                reply_queue.append(reply_url)

        def _enqueue_partials(partials: list[dict[str, Any]]) -> None:
            for partial in partials:
                src = partial.get("src")
                cursor = partial.get("cursor")
                if not src or not cursor:
                    continue
                key = f"{strip_query_and_fragment(src)}|{cursor}"
                if key in visited_partial_keys or key in queued_partial_keys:
                    continue
                queued_partial_keys.add(key)
                partial_queue.append({"src": src, "cursor": cursor})

        _enqueue(payload.get("more_reply_links", []))
        _enqueue_partials(payload.get("more_reply_partials", []))

        partial_requests_visited = 0
        reply_pages_visited = 0
        should_run_dom_fallback = (
            expand_replies
            and (
                not json_fetch_used
                or (
                    reported_comment_count is not None
                    and len(comments_by_id) < reported_comment_count
                )
            )
        )
        if should_run_dom_fallback:
            while (
                partial_queue or reply_queue
            ) and (morechildren_requests + partial_requests_visited + reply_pages_visited) < reply_pages_limit:
                if partial_queue:
                    partial = partial_queue.pop(0)
                    partial_key = f"{strip_query_and_fragment(partial['src'])}|{partial['cursor']}"
                    queued_partial_keys.discard(partial_key)
                    if partial_key in visited_partial_keys:
                        continue
                    visited_partial_keys.add(partial_key)
                    partial_payload = await fetch_more_comments_payload(
                        page,
                        partial["src"],
                        partial["cursor"],
                    )
                    if not partial_payload.get("ok"):
                        continue
                    for comment_item in partial_payload.get("comments", []):
                        _add_comment(comment_from_payload_item(comment_item, debug=debug))
                    _enqueue_partials(partial_payload.get("more_reply_partials", []))
                    partial_requests_visited += 1
                    continue

                reply_url = reply_queue.pop(0)
                canonical = strip_query_and_fragment(reply_url)
                queued_reply_urls.discard(canonical)
                if canonical in visited_reply_urls:
                    continue
                visited_reply_urls.add(canonical)
                reply_page = await context.new_page()
                try:
                    reply_payload = await capture_post_payload(reply_page, reply_url, comment_sort)
                finally:
                    await reply_page.close()
                if reply_payload["blocked"]:
                    continue
                for comment_item in reply_payload["comments"]:
                    _add_comment(comment_from_payload_item(comment_item, debug=debug))
                _enqueue(reply_payload.get("more_reply_links", []))
                _enqueue_partials(reply_payload.get("more_reply_partials", []))
                reply_pages_visited += 1

        children_by_parent: dict[str, list[str]] = {}
        for comment_id in comment_order:
            parent_id = comments_by_id[comment_id].get("parent_id")
            if not parent_id:
                continue
            children_by_parent.setdefault(parent_id, []).append(comment_id)

        for comment_id in comment_order:
            direct_reply_ids = children_by_parent.get(comment_id, [])
            comments_by_id[comment_id]["direct_reply_ids"] = direct_reply_ids
            comments_by_id[comment_id]["direct_reply_count"] = len(direct_reply_ids)
            comments_by_id[comment_id]["is_top_level"] = comments_by_id[comment_id].get("parent_id") is None

        comments = [
            comments_by_id[comment_id]
            for comment_id in comment_order
            if matches_date_window(
                comments_by_id[comment_id].get("created"),
                comment_start_date,
                comment_end_date,
            )
        ]
        if comments_limit > 0:
            comments = comments[:comments_limit]

        missing_comment_count = None
        if reported_comment_count is not None:
            missing_comment_count = max(reported_comment_count - len(comments_by_id), 0)

        return {
            "url": strip_query_and_fragment(post_attrs.get("content-href") or payload["final_url"]),
            "blocked": False,
            "id": post_attrs.get("id"),
            "title": post_attrs.get("post-title"),
            "author": post_attrs.get("author"),
            "author_id": post_attrs.get("author-id"),
            "subreddit": post_attrs.get("subreddit-prefixed-name"),
            "subreddit_name": post_attrs.get("subreddit-name"),
            "subreddit_id": post_attrs.get("subreddit-id"),
            "created_timestamp": post_attrs.get("created-timestamp"),
            "score": post_attrs.get("score"),
            "comment_count_reported": post_attrs.get("comment-count"),
            "visible_comment_count": payload.get("visible_comment_count"),
            "award_count": post_attrs.get("award-count"),
            "post_type": post_attrs.get("post-type"),
            "domain": post_attrs.get("domain"),
            "language": post_attrs.get("post-language"),
            "moderation_state": post_attrs.get("item-state"),
            "body_text": compact_text(post_text),
            "header_text": compact_text(payload["post"]["header_text"]),
            "times": payload["times"],
            "visible_comments": comments,
            "comment_sort": comment_sort,
            "reply_expansion": {
                "enabled": expand_replies,
                "reply_pages_limit": reply_pages_limit,
                "json_fetch_used": json_fetch_used,
                "json_url": json_payload.get("json_url"),
                "json_parse_error": json_parse_error,
                "json_initial_comment_count": len(json_payload.get("raw_comments", [])),
                "json_initial_more_nodes": json_more_nodes_initial,
                "json_remaining_more_nodes": len(pending_morechildren_nodes),
                "morechildren_requests": morechildren_requests,
                "partial_requests_visited": partial_requests_visited,
                "reply_page_visits": reply_pages_visited,
                "initial_more_reply_links": len(payload.get("more_reply_links", [])),
                "initial_more_reply_partials": len(payload.get("more_reply_partials", [])),
                "remaining_reply_links": len(reply_queue),
                "remaining_reply_partials": len(partial_queue),
                "collected_comment_count": len(comments_by_id),
                "missing_comment_count": missing_comment_count,
            },
            "related_links": [
                {
                    "text": item["text"],
                    "url": strip_query_and_fragment(item["href"]),
                }
                for item in payload["related_links"]
                if is_reddit_related_link(item["href"])
            ],
        }
    finally:
        await page.close()


def is_reddit_related_link(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc in {"www.reddit.com", "reddit.com"} and "/comments/" in parsed.path


async def main() -> None:
    args = parse_args()
    start_date = parse_iso_date(args.start_date)
    end_date = parse_iso_date(args.end_date)
    comment_start_date = parse_iso_date(args.comment_start_date)
    comment_end_date = parse_iso_date(args.comment_end_date)
    browser_executable = pick_browser_executable(args.browser_executable)
    browser, context = await launch_browser(browser_executable, args.headful)
    try:
        search_page = await context.new_page()
        if args.engine == "duckduckgo":
            search_url, search_results = await search_duckduckgo(
                search_page, args.subreddit, args.query, args.time,
            )
        else:
            search_url, search_results = await search_reddit(
                search_page, args.subreddit, args.query, args.time,
            )

        posts: list[dict[str, Any]] = []
        candidates_scanned = 0
        for result in search_results:
            if len(posts) >= args.limit:
                break
            candidates_scanned += 1
            post = await extract_post(
                context,
                result["url"],
                args.comments_limit,
                args.comment_sort,
                args.expand_replies,
                args.reply_pages_limit,
                comment_start_date,
                comment_end_date,
                args.debug,
            )
            if post.get("blocked"):
                continue
            if not matches_date_window(post.get("created_timestamp"), start_date, end_date):
                continue
            post["search"] = {
                key: value
                for key, value in result.items()
                if args.debug or key != "raw_search_text"
            }
            posts.append(post)
            await search_page.wait_for_timeout(args.wait_ms)

        output = {
            "browser_executable": browser_executable,
            "engine": args.engine,
            "subreddit": args.subreddit,
            "query": args.query,
            "time": args.time,
            "post_date_filter": {
                "start_date": args.start_date,
                "end_date": args.end_date,
            },
            "comment_controls": {
                "comment_sort": args.comment_sort,
                "comments_limit": args.comments_limit,
                "comment_start_date": args.comment_start_date,
                "comment_end_date": args.comment_end_date,
                "expand_replies": args.expand_replies,
                "reply_pages_limit": args.reply_pages_limit,
            },
            "search_url": search_url,
            "search_result_count": len(search_results),
            "candidates_scanned": candidates_scanned,
            "search_results": [
                {
                    key: value
                    for key, value in result.items()
                    if args.debug or key != "raw_search_text"
                }
                for result in search_results[: args.limit]
            ],
            "posts": posts,
        }

        output_json = json.dumps(output, indent=2)
        if args.output:
            Path(args.output).write_text(output_json + "\n")
        else:
            print(output_json)
    finally:
        await close_browser(browser, context)


if __name__ == "__main__":
    asyncio.run(main())
