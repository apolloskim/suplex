from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import ParseResult, urlencode, urlparse, urlunparse

from playwright.async_api import Page

from suplex.reddit_extract import (
    close_browser,
    extract_post,
    fetch_browser_response,
    isoformat_utc_timestamp,
    launch_browser,
    pick_browser_executable,
    strip_query_and_fragment,
)
from suplex.store.db import LeadStore


POLICY_PRESETS: dict[str, dict[str, Any]] = {
    "conservative": {
        "hydrate_mode": "rules",
        "min_age_minutes": 60,
        "hydrate_min_score": 5,
        "hydrate_min_comments": 3,
        "revisit_after_minutes": [120, 720, 2880],
    },
    "balanced": {
        "hydrate_mode": "rules",
        "min_age_minutes": 30,
        "hydrate_min_score": 3,
        "hydrate_min_comments": 2,
        "revisit_after_minutes": [60, 360, 1440],
    },
    "aggressive": {
        "hydrate_mode": "rules",
        "min_age_minutes": 10,
        "hydrate_min_score": 1,
        "hydrate_min_comments": 1,
        "revisit_after_minutes": [30, 120, 480],
    },
}

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch, revisit, and hydrate Reddit posts via Playwright-authenticated Reddit requests.",
    )
    parser.add_argument(
        "--mode",
        choices=["watch", "revisit", "hydrate"],
        default="watch",
        help="Pipeline stage to run.",
    )
    parser.add_argument("--subreddit", help="Subreddit name without r/. Required for watch mode.")
    parser.add_argument(
        "--sort",
        choices=["best", "hot", "new", "top", "rising"],
        default="new",
        help="Subreddit feed sort, matching the main Reddit UI choices.",
    )
    parser.add_argument(
        "--time",
        choices=["hour", "day", "week", "month", "year", "all"],
        default="month",
        help="Time window for top-sorted feeds.",
    )
    parser.add_argument(
        "--query",
        default="",
        help="Optional case-insensitive substring match against title + selftext.",
    )
    parser.add_argument(
        "--policy",
        choices=sorted(POLICY_PRESETS),
        default=None,
        help="Override the revisit/hydration policy preset.",
    )
    parser.add_argument(
        "--hydrate-mode",
        choices=["off", "manual", "rules"],
        default=None,
        help="Override hydration behavior. manual means only explicit hydrate commands extract posts.",
    )
    parser.add_argument(
        "--min-age-minutes",
        type=int,
        default=None,
        help="Minimum post age before rules-based hydration is allowed.",
    )
    parser.add_argument(
        "--hydrate-min-score",
        type=int,
        default=None,
        help="Rules-based hydration threshold for score.",
    )
    parser.add_argument(
        "--hydrate-min-comments",
        type=int,
        default=None,
        help="Rules-based hydration threshold for comment count.",
    )
    parser.add_argument(
        "--revisit-after-minutes",
        default=None,
        help="Comma-separated revisit schedule in minutes, e.g. 15,60,360.",
    )
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=30,
        help="Seconds between polls in watch mode.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Max posts to fetch or process in a single run.",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=0,
        help="Stop watch mode after this many polls. 0 means run forever.",
    )
    parser.add_argument(
        "--state-file",
        help="Optional watcher state file for quick inspection/debugging.",
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
        "--post-id",
        action="append",
        default=[],
        help="Explicit post id to hydrate. Repeatable.",
    )
    parser.add_argument(
        "--comments-limit",
        type=int,
        default=25,
        help="Comments to keep when hydrating a post. Use 0 for all collected comments.",
    )
    parser.add_argument(
        "--comment-sort",
        choices=["best", "new", "top", "old", "controversial", "qa"],
        default="best",
        help="Comment order to request during hydration.",
    )
    parser.add_argument(
        "--expand-replies",
        action="store_true",
        help="Expand Reddit more-reply branches during hydration.",
    )
    parser.add_argument(
        "--reply-pages-limit",
        type=int,
        default=20,
        help="Max reply pages / morechildren fetches during hydration.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Include extra hydration debugging fields.",
    )
    args = parser.parse_args()
    if args.mode == "watch" and not args.subreddit:
        parser.error("--subreddit is required in watch mode")
    return args


def build_subreddit_feed_url(subreddit: str, sort: str, limit: int, time_window: str) -> str:
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&raw_json=1"
    if sort == "top":
        url += f"&t={time_window}"
    return url


def post_permalink(post: dict[str, Any]) -> str | None:
    permalink = post.get("permalink")
    if not permalink:
        return None
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    return f"https://www.reddit.com{permalink}"


def build_post_json_url(url: str) -> str:
    canonical = strip_query_and_fragment(url)
    parsed = urlparse(canonical)
    path = parsed.path.rstrip("/") + "/.json"
    return urlunparse(
        ParseResult(
            scheme=parsed.scheme or "https",
            netloc=parsed.netloc or "www.reddit.com",
            path=path,
            params="",
            query=urlencode({"raw_json": "1", "limit": "1", "depth": "0"}),
            fragment="",
        )
    )


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
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_feed_posts(text: str) -> list[dict[str, Any]]:
    payload = json.loads(text)
    children = payload.get("data", {}).get("children", [])
    posts: list[dict[str, Any]] = []
    for child in children:
        if child.get("kind") != "t3":
            continue
        data = child.get("data") or {}
        thing_id = data.get("name") or data.get("id")
        if not thing_id:
            continue
        posts.append(
            {
                "id": thing_id,
                "short_id": str(thing_id).removeprefix("t3_"),
                "subreddit": data.get("subreddit"),
                "title": data.get("title"),
                "author": data.get("author"),
                "created_timestamp": isoformat_utc_timestamp(data.get("created_utc")),
                "permalink": data.get("permalink"),
                "url": post_permalink(data),
                "score": int(data.get("score") or 0),
                "num_comments": int(data.get("num_comments") or 0),
                "selftext": data.get("selftext") or "",
                "is_self": bool(data.get("is_self")),
                "domain": data.get("domain"),
                "link_flair_text": data.get("link_flair_text"),
            },
        )
    return posts


def parse_post_summary(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    children = payload[0].get("data", {}).get("children", []) if isinstance(payload, list) and payload else []
    if not children:
        raise ValueError("No post payload found")
    data = children[0].get("data") or {}
    thing_id = data.get("name") or data.get("id")
    if not thing_id:
        raise ValueError("Post payload missing id")
    return {
        "id": thing_id,
        "short_id": str(thing_id).removeprefix("t3_"),
        "subreddit": data.get("subreddit"),
        "title": data.get("title"),
        "author": data.get("author"),
        "created_timestamp": isoformat_utc_timestamp(data.get("created_utc")),
        "permalink": data.get("permalink"),
        "url": post_permalink(data),
        "score": int(data.get("score") or 0),
        "num_comments": int(data.get("num_comments") or 0),
        "selftext": data.get("selftext") or "",
        "is_self": bool(data.get("is_self")),
        "domain": data.get("domain"),
        "link_flair_text": data.get("link_flair_text"),
    }


def matches_query(post: dict[str, Any], query: str) -> bool:
    query = (query or "").strip().lower()
    if not query:
        return True
    haystack = f"{post.get('title') or ''}\n{post.get('selftext') or ''}".lower()
    return query in haystack


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"seen_ids": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"seen_ids": []}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def compact_post(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": post["id"],
        "title": post.get("title"),
        "author": post.get("author"),
        "created_timestamp": post.get("created_timestamp"),
        "num_comments": post.get("num_comments"),
        "score": post.get("score"),
        "url": post.get("url"),
    }


def parse_minutes_csv(value: str | None) -> list[int]:
    if not value:
        return []
    minutes = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        minutes.append(int(item))
    return sorted(set(m for m in minutes if m > 0))


def resolve_policy(args: argparse.Namespace, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    if args.policy is None:
        policy = dict(POLICY_PRESETS["balanced"])
        if fallback:
            policy.update(fallback)
    else:
        policy = dict(POLICY_PRESETS.get(args.policy, POLICY_PRESETS["balanced"]))
    if args.hydrate_mode is not None:
        policy["hydrate_mode"] = args.hydrate_mode
    if args.min_age_minutes is not None:
        policy["min_age_minutes"] = args.min_age_minutes
    if args.hydrate_min_score is not None:
        policy["hydrate_min_score"] = args.hydrate_min_score
    if args.hydrate_min_comments is not None:
        policy["hydrate_min_comments"] = args.hydrate_min_comments
    revisit_after = parse_minutes_csv(args.revisit_after_minutes)
    if revisit_after:
        policy["revisit_after_minutes"] = revisit_after
    return policy


def next_revisit_at(
    created_timestamp: str | None,
    now: datetime,
    revisit_after_minutes: list[int],
) -> str | None:
    created_at = parse_timestamp(created_timestamp)
    if not created_at:
        return None
    for minutes in revisit_after_minutes:
        revisit_time = created_at + timedelta(minutes=minutes)
        if revisit_time > now:
            return revisit_time.isoformat()
    return None


def evaluate_post_action(
    post: dict[str, Any],
    *,
    now: datetime,
    policy: dict[str, Any],
) -> tuple[str, str | None]:
    hydrate_mode = policy.get("hydrate_mode", "rules")
    revisit_schedule = list(policy.get("revisit_after_minutes", []))
    revisit_at = next_revisit_at(post.get("created_timestamp"), now, revisit_schedule)
    if hydrate_mode in {"off", "manual"}:
        if revisit_at:
            return "revisit", revisit_at
        return "discard", None

    created_at = parse_timestamp(post.get("created_timestamp"))
    age_minutes = 0.0
    if created_at:
        age_minutes = max((now - created_at).total_seconds() / 60.0, 0.0)
    score = int(post.get("score") or 0)
    num_comments = int(post.get("num_comments") or 0)
    if (
        age_minutes >= int(policy.get("min_age_minutes", 30))
        and (
            score >= int(policy.get("hydrate_min_score", 3))
            or num_comments >= int(policy.get("hydrate_min_comments", 2))
        )
    ):
        return "hydrate", None
    if revisit_at:
        return "revisit", revisit_at
    return "discard", None


async def prime_reddit_session(page: Page, subreddit: str, sort: str) -> dict[str, Any]:
    url = f"https://www.reddit.com/r/{subreddit}/{sort}/"
    await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    await page.wait_for_timeout(3_000)
    title = await page.title()
    return {
        "url": page.url,
        "title": title,
        "blocked": "Prove your humanity" in title,
    }


async def fetch_live_feed(page: Page, subreddit: str, sort: str, limit: int, time_window: str) -> dict[str, Any]:
    response = await fetch_browser_response(page, build_subreddit_feed_url(subreddit, sort, limit, time_window))
    result = {
        "posts": [],
        **response,
    }
    if not response.get("ok"):
        return result
    try:
        result["posts"] = parse_feed_posts(response["text"])
    except json.JSONDecodeError as exc:
        result["parse_error"] = str(exc)
    return result


async def fetch_post_summary(page: Page, url: str) -> dict[str, Any]:
    response = await fetch_browser_response(page, build_post_json_url(url))
    result = {
        "post": None,
        **response,
    }
    if not response.get("ok"):
        return result
    try:
        result["post"] = parse_post_summary(response["text"])
    except (json.JSONDecodeError, ValueError) as exc:
        result["parse_error"] = str(exc)
    return result


def remember_state(state_path: Path | None, seen_ids: set[str], *, poll_count: int, updated_at: str) -> None:
    if not state_path:
        return
    save_state(
        state_path,
        {
            "seen_ids": list(seen_ids)[-1000:],
            "updated_at": updated_at,
            "poll_count": poll_count,
        },
    )


async def run_watch(args: argparse.Namespace) -> int:
    job_id = os.environ.get("SUPLEX_JOB_ID")
    store = LeadStore()
    state_path = Path(args.state_file) if args.state_file else None
    state = load_state(state_path) if state_path else {"seen_ids": []}
    seen_ids = set(state.get("seen_ids", []))
    browser_executable = pick_browser_executable(args.browser_executable)
    policy = resolve_policy(args)
    browser, context = await launch_browser(browser_executable, args.headful)
    poll_count = 0
    print(
        json.dumps(
            {
                "event": "watch_started",
                "subreddit": args.subreddit,
                "sort": args.sort,
                "time": args.time,
                "query": args.query,
                "policy": policy,
                "interval_seconds": args.interval_seconds,
                "limit": args.limit,
                "browser_executable": browser_executable,
                "state_file": str(state_path) if state_path else None,
            }
        ),
        flush=True,
    )

    try:
        page = await context.new_page()
        prime = await prime_reddit_session(page, args.subreddit, args.sort)
        print(json.dumps({"event": "session_primed", **prime}), flush=True)
        if prime["blocked"]:
            if job_id:
                store.finish_job(job_id, "failed", error="reddit_humanity_check")
            return 1

        while True:
            poll_count += 1
            now = datetime.now(timezone.utc)
            now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%f+0000")
            feed = await fetch_live_feed(page, args.subreddit, args.sort, args.limit, args.time)

            new_match_count = 0
            queued_hydration = 0
            queued_revisit = 0
            discarded = 0

            for post in feed.get("posts", []):
                matched = matches_query(post, args.query)
                store.upsert_reddit_post(
                    post,
                    seen_at=now_iso,
                    query=args.query,
                    sort=args.sort,
                    time_window=args.time,
                    matched=matched,
                    policy=policy,
                )
                store.insert_reddit_post_snapshot(
                    post["id"],
                    seen_at=now_iso,
                    sort=args.sort,
                    time_window=args.time,
                    score=int(post.get("score") or 0),
                    num_comments=int(post.get("num_comments") or 0),
                    snapshot=post,
                )
                if not matched:
                    continue
                if post["id"] not in seen_ids:
                    new_match_count += 1
                seen_ids.add(post["id"])
                current = store.get_reddit_post(post["id"])
                if current and current.get("hydration_status") in {"queued", "hydrated"}:
                    continue
                action, revisit_at = evaluate_post_action(post, now=now, policy=policy)
                if action == "hydrate":
                    store.queue_reddit_post_hydration(post["id"])
                    queued_hydration += 1
                elif action == "revisit":
                    store.schedule_reddit_post_revisit(post["id"], next_revisit_at=revisit_at)
                    queued_revisit += 1
                else:
                    store.mark_reddit_post_discarded(post["id"])
                    discarded += 1
                print(
                    json.dumps(
                        {
                            "event": "match",
                            **compact_post(post),
                            "action": action,
                            "next_revisit_at": revisit_at,
                        }
                    ),
                    flush=True,
                )

            remember_state(state_path, seen_ids, poll_count=poll_count, updated_at=now_iso)

            if job_id:
                store.update_job_progress(job_id, poll_count, len(seen_ids))

            summary = {
                "event": "poll",
                "timestamp": now_iso,
                "subreddit": args.subreddit,
                "sort": args.sort,
                "time": args.time,
                "query": args.query,
                "policy": policy,
                "poll": poll_count,
                "status": feed.get("status"),
                "ok": feed.get("ok"),
                "post_count": len(feed.get("posts", [])),
                "new_match_count": new_match_count,
                "queued_hydration": queued_hydration,
                "queued_revisit": queued_revisit,
                "discarded": discarded,
            }
            if feed.get("parse_error"):
                summary["parse_error"] = feed["parse_error"]
            print(json.dumps(summary), flush=True)

            if args.max_cycles and poll_count >= args.max_cycles:
                if job_id:
                    store.finish_job(job_id, "done")
                print(json.dumps({"event": "watch_finished", "reason": "max_cycles"}), flush=True)
                return 0

            await page.wait_for_timeout(args.interval_seconds * 1000)
    finally:
        await close_browser(browser, context)


async def run_revisit(args: argparse.Namespace) -> int:
    job_id = os.environ.get("SUPLEX_JOB_ID")
    store = LeadStore()
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    posts = store.get_due_reddit_revisits(
        now_iso=now_iso,
        subreddit=args.subreddit,
        limit=args.limit,
    )
    if not posts:
        print(json.dumps({"event": "revisit_finished", "reason": "no_due_posts"}), flush=True)
        if job_id:
            store.finish_job(job_id, "done")
        return 0

    browser_executable = pick_browser_executable(args.browser_executable)
    browser, context = await launch_browser(browser_executable, args.headful)
    processed = 0
    hydrated = 0
    rescheduled = 0
    discarded = 0

    try:
        page = await context.new_page()
        prime = await prime_reddit_session(page, posts[0]["subreddit"], "new")
        print(json.dumps({"event": "session_primed", **prime}), flush=True)
        if prime["blocked"]:
            if job_id:
                store.finish_job(job_id, "failed", error="reddit_humanity_check")
            return 1

        for stored_post in posts:
            policy = resolve_policy(args, fallback=stored_post.get("policy_json", {}))
            summary = await fetch_post_summary(page, stored_post["url"])
            processed += 1
            if not summary.get("ok") or not summary.get("post"):
                print(
                    json.dumps(
                        {
                            "event": "revisit_error",
                            "post_id": stored_post["post_id"],
                            "url": stored_post["url"],
                            "status": summary.get("status"),
                            "parse_error": summary.get("parse_error"),
                        }
                    ),
                    flush=True,
                )
                continue

            post = summary["post"]
            seen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+0000")
            query = stored_post.get("matched_query", "")
            store.upsert_reddit_post(
                post,
                seen_at=seen_at,
                query=query,
                sort=stored_post.get("last_sort", "new"),
                time_window=stored_post.get("last_time_window", "month"),
                matched=bool(stored_post.get("matched")),
                policy=policy,
            )
            store.insert_reddit_post_snapshot(
                post["id"],
                seen_at=seen_at,
                sort=stored_post.get("last_sort", "new"),
                time_window=stored_post.get("last_time_window", "month"),
                score=int(post.get("score") or 0),
                num_comments=int(post.get("num_comments") or 0),
                snapshot=post,
            )

            action, revisit_at = evaluate_post_action(post, now=datetime.now(timezone.utc), policy=policy)
            if action == "hydrate":
                store.queue_reddit_post_hydration(post["id"])
                hydrated += 1
            elif action == "revisit":
                store.record_reddit_post_revisit(
                    post["id"],
                    next_revisit_at=revisit_at,
                    signal_status="watching",
                )
                rescheduled += 1
            else:
                store.record_reddit_post_revisit(
                    post["id"],
                    next_revisit_at=None,
                    signal_status="discarded",
                )
                discarded += 1

            print(
                json.dumps(
                    {
                        "event": "revisit",
                        **compact_post(post),
                        "action": action,
                        "next_revisit_at": revisit_at,
                    }
                ),
                flush=True,
            )

            if job_id:
                store.update_job_progress(job_id, processed, hydrated)

        if job_id:
            store.finish_job(job_id, "done")
        print(
            json.dumps(
                {
                    "event": "revisit_finished",
                    "processed": processed,
                    "queued_for_hydration": hydrated,
                    "rescheduled": rescheduled,
                    "discarded": discarded,
                }
            ),
            flush=True,
        )
        return 0
    finally:
        await close_browser(browser, context)


async def run_hydrate(args: argparse.Namespace) -> int:
    job_id = os.environ.get("SUPLEX_JOB_ID")
    store = LeadStore()

    explicit_ids = [item for item in args.post_id if item]
    posts: list[dict] = []
    if explicit_ids:
        for post_id in explicit_ids:
            post = store.get_reddit_post(post_id)
            if post:
                posts.append(post)
    else:
        posts = store.get_queued_reddit_hydrations(
            subreddit=args.subreddit,
            limit=args.limit,
        )

    if not posts:
        print(json.dumps({"event": "hydrate_finished", "reason": "no_posts"}), flush=True)
        if job_id:
            store.finish_job(job_id, "done")
        return 0

    browser_executable = pick_browser_executable(args.browser_executable)
    browser, context = await launch_browser(browser_executable, args.headful)
    processed = 0
    hydrated = 0
    failed = 0

    try:
        for stored_post in posts[: args.limit]:
            processed += 1
            try:
                payload = await extract_post(
                    context,
                    stored_post["url"],
                    args.comments_limit,
                    args.comment_sort,
                    args.expand_replies,
                    args.reply_pages_limit,
                    None,
                    None,
                    args.debug,
                )
                if payload.get("blocked"):
                    raise RuntimeError("reddit_humanity_check")
                store.mark_reddit_post_hydrated(stored_post["post_id"], payload)
                hydrated += 1
                print(
                    json.dumps(
                        {
                            "event": "hydrated",
                            "post_id": stored_post["post_id"],
                            "url": stored_post["url"],
                            "title": payload.get("title"),
                            "collected_comment_count": payload.get("reply_expansion", {}).get("collected_comment_count"),
                        }
                    ),
                    flush=True,
                )
            except Exception as exc:
                store.mark_reddit_post_hydration_failed(stored_post["post_id"], str(exc))
                failed += 1
                print(
                    json.dumps(
                        {
                            "event": "hydrate_error",
                            "post_id": stored_post["post_id"],
                            "url": stored_post["url"],
                            "error": str(exc),
                        }
                    ),
                    flush=True,
                )

            if job_id:
                store.update_job_progress(job_id, processed, hydrated)

        if job_id:
            store.finish_job(job_id, "done" if failed == 0 else "done_with_errors")
        print(
            json.dumps(
                {
                    "event": "hydrate_finished",
                    "processed": processed,
                    "hydrated": hydrated,
                    "failed": failed,
                }
            ),
            flush=True,
        )
        return 0 if failed == 0 else 1
    finally:
        await close_browser(browser, context)


async def main() -> None:
    args = parse_args()
    if args.mode == "watch":
        code = await run_watch(args)
    elif args.mode == "revisit":
        code = await run_revisit(args)
    else:
        code = await run_hydrate(args)
    raise SystemExit(code)


if __name__ == "__main__":
    asyncio.run(main())
