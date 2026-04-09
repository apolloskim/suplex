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

    # Pre-create job row with meta so resume can reconstruct the command.
    meta = {"type": "scrape", "source": source, "query": query, "location": location}
    if extra_args:
        meta["extra_args"] = extra_args
    store = LeadStore()
    store.create_job(job_id, "scrape", total=0, meta=meta)

    env = {**os.environ, "SUPLEX_JOB_ID": job_id}
    return _launch(job_id, cmd, env, log_path, meta=meta)


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

    # Spawn watchdog — auto-advances the batch when slots open.
    _spawn_watchdog(batch_id, max_parallel)

    logger.info("Batch %s: %d jobs, %d launched + watchdog", batch_id, len(specs), len(launched))
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


def _meta_to_extra_args(job_type: str, meta: dict) -> list[str]:
    """Convert batch child meta to CLI extra_args, branched by job type."""
    extra: list[str] = []
    # Shared flags.
    if meta.get("only"):
        extra.extend(["--only", meta["only"]])
    if meta.get("exclude"):
        extra.extend(["--exclude", meta["exclude"]])

    if job_type == "scrape_maps":
        if meta.get("scraper"):
            extra.extend(["-s", meta["scraper"]])
    elif job_type == "scrape_linkedin":
        _LI_FLAG_MAP = {
            "titles": "-t", "past_titles": "--past-titles",
            "companies": "-c", "past_companies": "--past-companies",
            "industries": "--industries", "seniority": "--seniority",
            "functions": "--functions", "headcount": "--headcount",
            "schools": "--schools", "years_exp": "--years-exp",
            "years_at_company": "--years-at-company",
            "company_hq": "--company-hq",
            "profile_languages": "--profile-languages",
        }
        for key, flag in _LI_FLAG_MAP.items():
            if meta.get(key):
                extra.extend([flag, meta[key]])
        if meta.get("recently_changed_jobs"):
            extra.append("--recently-changed-jobs")
    return extra


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
        extra = _meta_to_extra_args(job_type, meta)
        if max_results > 0:
            extra.extend(["-n", str(max_results)])
        launch_scrape_job(source, query, location, max_results=max_results, extra_args=extra, job_id=job_id)
    elif job_type == "enrich":
        launch_enrich_job(
            limit=meta.get("limit", 100),
            linkedin_posts=meta.get("linkedin_posts", 1),
            job_id=job_id,
        )


def resume_job(job_id: str, scraper_override: str | None = None) -> dict:
    """Resume a stopped/failed job. Relaunches the same command with the same job_id.

    The coordinator will skip already-done tiles via item_tasks.
    scraper_override: switch from playwright to apify (or vice versa) on resume.
    """
    store = LeadStore()
    job = store.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}

    if job["status"] == "running":
        return {"error": "Job is already running", "job_id": job_id}

    if job["status"] == "done":
        counts = store.get_item_task_counts(job_id)
        failed = counts.get("failed", 0)
        if failed == 0:
            return {"error": "Job already completed with no failures", "job_id": job_id}

    # Reset failed tasks to pending so they get retried.
    with store._conn() as conn:
        conn.execute(
            "UPDATE item_tasks SET status = 'pending', attempts = attempts WHERE job_id = ? AND status = 'failed'",
            (job_id,),
        )
        reset = conn.execute(
            "SELECT changes()"
        ).fetchone()[0]

    store.reset_stale_tasks(job_id)

    meta = job["meta"]
    job_type = job["type"]

    if job_type == "scrape":
        source = meta.get("source", "maps")
        query = meta.get("query", "")
        location = meta.get("location", "")
        extra = list(meta.get("extra_args", []))
        # Apply scraper override (e.g., switch playwright → apify on resume).
        if scraper_override:
            # Remove old -s flag from extra_args.
            new_extra = []
            skip_next = False
            for arg in extra:
                if skip_next:
                    skip_next = False
                    continue
                if arg == "-s":
                    skip_next = True
                    continue
                new_extra.append(arg)
            new_extra.extend(["-s", scraper_override])
            extra = new_extra
        launch_scrape_job(source, query, location, extra_args=extra, job_id=job_id)
    elif job_type in ("scrape_maps", "scrape_linkedin"):
        # Batch child jobs — reconstruct extra_args from meta.
        source = "maps" if job_type == "scrape_maps" else "linkedin"
        query = meta.get("query", "")
        location = meta.get("location", "")
        extra = _meta_to_extra_args(job_type, meta)
        if scraper_override and job_type == "scrape_maps":
            extra = [a for i, a in enumerate(extra) if not (a == "-s" or (i > 0 and extra[i - 1] == "-s"))]
            extra.extend(["-s", scraper_override])
        max_results = meta.get("max_results", 0)
        if max_results > 0:
            extra.extend(["-n", str(max_results)])
        launch_scrape_job(source, query, location, extra_args=extra, job_id=job_id)
    elif job_type == "enrich":
        launch_enrich_job(
            limit=meta.get("limit", 100),
            linkedin_posts=meta.get("linkedin_posts", 1),
            job_id=job_id,
        )
    else:
        return {"error": f"Cannot resume job type: {job_type}"}

    counts = store.get_item_task_counts(job_id)
    return {
        "job_id": job_id,
        "status": "resumed",
        "tasks_reset": reset,
        "tasks": counts,
    }


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


def _spawn_watchdog(batch_id: str, max_parallel: int) -> None:
    """Spawn a detached watchdog process that auto-advances the batch."""
    cmd = [
        sys.executable, "-c",
        f"from suplex.jobs import _watchdog_loop; _watchdog_loop('{batch_id}', {max_parallel})",
    ]
    log_path = _log_path(f"{batch_id}_watchdog")
    log_file = open(log_path, "w")
    subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _watchdog_loop(batch_id: str, max_parallel: int, poll_interval: int = 15) -> None:
    """Check every poll_interval seconds and launch next jobs when slots open.

    Runs as a detached subprocess. Exits when batch is complete.
    """
    import time

    store = LeadStore()

    while True:
        status = store.get_batch_status(batch_id)
        if not status:
            break

        done = status["done"] + status["failed"]
        total = status["total_jobs"]

        # Batch complete?
        if done >= total:
            final_status = "done" if status["failed"] == 0 else "done_with_errors"
            store.finish_job(batch_id, final_status)
            print(f"Batch {batch_id} complete: {final_status} ({status['done']} done, {status['failed']} failed)")
            break

        # Launch next jobs if slots open.
        running = store.count_running_children(batch_id)
        slots = max(0, max_parallel - running)

        if slots > 0 and status["pending"] > 0:
            pending = store.get_pending_children(batch_id, limit=slots)
            for child in pending:
                meta = json.loads(child["meta"]) if isinstance(child["meta"], str) else child["meta"]
                _launch_child_job({"job_id": child["id"], "type": child["type"], "meta": meta})
                print(f"Launched {child['id']} ({meta.get('query', '')} / {meta.get('location', '')})")

        time.sleep(poll_interval)


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
