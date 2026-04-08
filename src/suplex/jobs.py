"""Background job launcher — starts scrape/enrich as detached processes.

The job runs in a separate process that survives terminal closure.
Progress is tracked in SQLite (jobs table) — any process can read it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

LOG_DIR = Path.home() / ".config" / "suplex" / "logs"


def launch_scrape_job(
    source: str,
    query: str,
    location: str,
    *,
    max_results: int = 0,
    extra_args: list[str] | None = None,
    job_id: str | None = None,
) -> dict:
    """Launch a scrape job in the background. Returns job info immediately."""
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)

    cmd = [
        _suplex_bin(), "scrape", source, query,
        "-l", location,
    ]
    if max_results > 0:
        cmd.extend(["-n", str(max_results)])
    if extra_args:
        cmd.extend(extra_args)

    # Inject job_id via env var — the CLI reads it and passes to coordinator.
    env = {**os.environ, "SUPLEX_JOB_ID": job_id}

    return _launch(job_id, cmd, env, log_path, meta={
        "type": "scrape", "source": source, "query": query, "location": location,
    })


def launch_enrich_job(
    *,
    limit: int = 100,
    linkedin_posts: int = 1,
    job_id: str | None = None,
) -> dict:
    """Launch an enrichment job in the background."""
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)

    cmd = [
        _suplex_bin(), "enrich",
        "-n", str(limit),
        "-lp", str(linkedin_posts),
    ]

    env = {**os.environ, "SUPLEX_JOB_ID": job_id}

    return _launch(job_id, cmd, env, log_path, meta={
        "type": "enrich", "limit": limit, "linkedin_posts": linkedin_posts,
    })


def launch_batch_job(specs: list[dict], *, max_parallel: int = 3) -> dict:
    """Launch a batch of scrape/enrich jobs with concurrency control.

    Each spec: {"type": "scrape_maps"|"scrape_linkedin"|"enrich", "query": ..., "location": ..., ...}
    """
    batch_id = _gen_id()
    store = LeadStore()

    # Build child specs with job IDs.
    child_specs = []
    for spec in specs:
        child_id = _gen_id()
        child_specs.append({
            "job_id": child_id,
            "type": spec["type"],
            "meta": spec,
        })

    store.create_batch_job(batch_id, child_specs, meta={"total_specs": len(specs)})

    # Launch first N children.
    launched = []
    for child in child_specs[:max_parallel]:
        _launch_child_job(child)
        launched.append(child["job_id"])

    logger.info("Batch %s: %d jobs, %d launched", batch_id, len(specs), len(launched))
    return {
        "batch_id": batch_id,
        "total_jobs": len(specs),
        "launched": len(launched),
        "max_parallel": max_parallel,
    }


def advance_batch(batch_id: str, max_parallel: int = 3) -> dict:
    """Check batch status and launch next pending children if slots are open."""
    store = LeadStore()
    status = store.get_batch_status(batch_id)
    if not status:
        return {"error": f"Batch {batch_id} not found"}

    running = status["running"]
    slots = max(0, max_parallel - running)
    newly_launched = []

    if slots > 0 and status["pending"] > 0:
        pending = store.get_pending_children(batch_id, limit=slots)
        for child in pending:
            meta = json.loads(child["meta"]) if isinstance(child["meta"], str) else child["meta"]
            _launch_child_job({"job_id": child["id"], "type": child["type"], "meta": meta})
            newly_launched.append(child["id"])

    # Check if batch is complete.
    if status["done"] + status["failed"] == status["total_jobs"]:
        store.finish_job(batch_id, "done" if status["failed"] == 0 else "done_with_errors")

    return {**status, "newly_launched": len(newly_launched)}


def get_batch_status(batch_id: str) -> dict:
    """Get batch status with aggregated progress."""
    store = LeadStore()
    status = store.get_batch_status(batch_id)
    if not status:
        return {"error": f"Batch {batch_id} not found"}
    return status


def _launch_child_job(child: dict) -> None:
    """Launch a single child job from a batch."""
    meta = child.get("meta", {})
    if isinstance(meta, str):
        meta = json.loads(meta)
    job_type = child["type"]
    job_id = child["job_id"]

    # Mark as running (row already exists from create_batch_job).
    store = LeadStore()
    with store._conn() as conn:
        conn.execute("UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
                     (datetime.utcnow().isoformat(), job_id))

    if job_type in ("scrape_maps", "scrape_linkedin"):
        source = "maps" if job_type == "scrape_maps" else "linkedin"
        query = meta.get("query", "")
        location = meta.get("location", "")
        max_results = meta.get("max_results", 0)
        extra = []
        if meta.get("scraper"):
            extra.extend(["-s", meta["scraper"]])
        if meta.get("titles"):
            extra.extend(["-t", meta["titles"]])
        if max_results > 0:
            extra.extend(["-n", str(max_results)])
        launch_scrape_job(source, query, location, max_results=max_results, extra_args=extra, job_id=job_id)
    elif job_type == "enrich":
        launch_enrich_job(
            limit=meta.get("limit", 100),
            linkedin_posts=meta.get("linkedin_posts", 1),
            job_id=job_id,
        )


def get_job_status(job_id: str) -> dict:
    """Get current status of a job from SQLite."""
    store = LeadStore()
    job = store.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}

    result = {
        "job_id": job["id"],
        "type": job["type"],
        "status": job["status"],
        "completed": job["completed"],
        "total": job["total"],
        "leads_count": job["leads_count"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
    }

    if job["total"] > 0:
        result["progress_pct"] = round(job["completed"] / job["total"] * 100, 1)

    if job["error"]:
        result["error"] = job["error"]

    result["meta"] = job["meta"]
    return result


def list_jobs(status: str | None = None) -> list[dict]:
    """List recent jobs."""
    store = LeadStore()
    return store.list_jobs(status=status)


def _suplex_bin() -> str:
    """Find the suplex executable — either installed entry point or python -m."""
    # Check if 'suplex' is on PATH (installed via pip/uv).
    suplex = shutil.which("suplex")
    if suplex:
        return suplex
    # Fallback: use the same Python that's running us.
    return sys.executable  # Will need __main__.py


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


def _log_path(job_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{job_id}.log"


def _launch(job_id: str, cmd: list[str], env: dict, log_path: Path, meta: dict) -> dict:
    """Spawn a detached subprocess."""
    log_file = open(log_path, "w")

    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # Detach from terminal.
        env=env,
    )

    logger.info("Launched job %s (pid=%d): %s", job_id, proc.pid, " ".join(cmd))

    return {
        "job_id": job_id,
        "pid": proc.pid,
        "log": str(log_path),
        "status": "launched",
        **meta,
    }
