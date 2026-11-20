from __future__ import annotations

import logging
from typing import Any

from apify_client import ApifyClientAsync

logger = logging.getLogger(__name__)


class ApifyError(RuntimeError):
    pass


async def run_actor(token: str, actor_id: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
    if not token:
        raise ApifyError(
            f"No Apify token configured, so {actor_id} cannot run. "
            "Run `suplex setup` or set APIFY_TOKEN."
        )

    client = ApifyClientAsync(token)
    run = await client.actor(actor_id).call(run_input=run_input)
    if run is None:
        raise ApifyError(f"Apify did not return a run for actor {actor_id}")

    status = run.get("status")
    if status != "SUCCEEDED":
        logger.warning("Apify actor %s finished with status %s — using partial results", actor_id, status)

    page = await client.dataset(run["defaultDatasetId"]).list_items()
    logger.info("Apify actor %s returned %d items", actor_id, len(page.items))
    return page.items
