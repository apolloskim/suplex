from __future__ import annotations

import logging

import httpx

from suplex.config import Config

logger = logging.getLogger(__name__)


async def get_apify_budget(token: str | None = None) -> dict:
    token = token or Config.load().apify_token
    if not token:
        return {"error": "No Apify token configured"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.apify.com/v2/users/me/limits",
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        return {"error": f"Could not reach Apify: {exc}"}

    if resp.status_code == 401:
        return {"error": "Apify rejected the token (HTTP 401). Run `suplex setup` to replace it."}
    if resp.status_code != 200:
        return {"error": f"Apify API returned {resp.status_code}"}

    data = resp.json().get("data", {})

    limits = data.get("limits", {})
    current = data.get("current", {})
    cycle = data.get("monthlyUsageCycle", {})

    max_usd = limits.get("maxMonthlyUsageUsd", 0)
    used_usd = current.get("monthlyUsageUsd", 0)
    remaining_usd = max(0, max_usd - used_usd)

    return {
        "plan_limit_usd": max_usd,
        "used_usd": round(used_usd, 2),
        "remaining_usd": round(remaining_usd, 2),
        "over_limit": used_usd > max_usd,
        "cycle_start": cycle.get("startAt", ""),
        "cycle_end": cycle.get("endAt", ""),
        "max_concurrent_runs": limits.get("maxConcurrentActorJobs", 32),
        "active_runs": current.get("activeActorJobCount", 0),
    }


def estimate_linkedin_cost(tile_count: int, results_per_tile: int = 25) -> dict:
    pages_per_tile = max(1, results_per_tile // 25)
    page_cost = tile_count * pages_per_tile * 0.10
    profile_cost = tile_count * results_per_tile * 0.01
    total = page_cost + profile_cost

    return {
        "tiles": tile_count,
        "estimated_pages": tile_count * pages_per_tile,
        "estimated_profiles": tile_count * results_per_tile,
        "page_cost_usd": round(page_cost, 2),
        "profile_cost_usd": round(profile_cost, 2),
        "total_estimated_usd": round(total, 2),
    }


def estimate_gmaps_cost(tile_count: int, results_per_tile: int = 100) -> dict:
    run_cost = tile_count * 0.007
    place_cost = tile_count * results_per_tile * 0.004
    total = run_cost + place_cost

    return {
        "tiles": tile_count,
        "estimated_places": tile_count * results_per_tile,
        "run_cost_usd": round(run_cost, 2),
        "place_cost_usd": round(place_cost, 2),
        "total_estimated_usd": round(total, 2),
    }
