"""Background job launcher — starts scrape/enrich as detached processes.

The job runs in a separate process that survives terminal closure.
Progress is tracked in SQLite (jobs table) — any process can read it.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psutil

from suplex.clock import utcnow
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

LOG_DIR = Path.home() / ".config" / "suplex" / "logs"
TERMINAL_JOB_STATUSES = {"done", "failed", "done_with_errors", "stopped"}
BATCH_CHILD_TYPES = {
    "scrape_maps", "scrape_linkedin", "enrich",
    "reddit_watch", "reddit_revisit", "reddit_hydrate",
}


def launch_scrape_job(
    source: str,
    query: str,
    location: str,
    *,
    extra_args: list[str] | None = None,
    job_id: str | None = None,
) -> dict:
    """Launch a scrape job in the background. Returns job info immediately."""
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)

    cmd = [*_suplex_cmd(), "scrape", source, query, "-l", location, *(extra_args or [])]

    # Pre-create job row with meta so resume can reconstruct the command.
    meta: dict[str, object] = {"type": "scrape", "source": source, "query": query, "location": location}
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

    cmd = [*_suplex_cmd(), "enrich", "-n", str(limit), "-lp", str(linkedin_posts)]
    meta = {"type": "enrich", "limit": limit, "linkedin_posts": linkedin_posts}
    LeadStore().create_job(job_id, "enrich", total=0, meta=meta)

    env = {**os.environ, "SUPLEX_JOB_ID": job_id}
    return _launch(job_id, cmd, env, log_path, meta=meta)


def launch_reddit_watch_job(
    subreddit: str,
    *,
    sort: str = "new",
    time_window: str = "month",
    query: str = "",
    policy: str = "balanced",
    hydrate_mode: str | None = None,
    min_age_minutes: int | None = None,
    hydrate_min_score: int | None = None,
    hydrate_min_comments: int | None = None,
    revisit_after_minutes: str | None = None,
    interval_seconds: int = 30,
    limit: int = 25,
    browser_executable: str | None = None,
    headful: bool = False,
    job_id: str | None = None,
) -> dict:
    """Launch a Reddit live-feed watcher in the background."""
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)
    state_path = _state_path(job_id)

    cmd = [
        sys.executable,
        "-m",
        "suplex.reddit_watch",
        "--mode",
        "watch",
        "--subreddit",
        subreddit,
        "--sort",
        sort,
        "--time",
        time_window,
        "--policy",
        policy,
        "--interval-seconds",
        str(interval_seconds),
        "--limit",
        str(limit),
        "--state-file",
        str(state_path),
    ]
    if query:
        cmd.extend(["--query", query])
    if hydrate_mode:
        cmd.extend(["--hydrate-mode", hydrate_mode])
    if min_age_minutes is not None:
        cmd.extend(["--min-age-minutes", str(min_age_minutes)])
    if hydrate_min_score is not None:
        cmd.extend(["--hydrate-min-score", str(hydrate_min_score)])
    if hydrate_min_comments is not None:
        cmd.extend(["--hydrate-min-comments", str(hydrate_min_comments)])
    if revisit_after_minutes:
        cmd.extend(["--revisit-after-minutes", revisit_after_minutes])
    if browser_executable:
        cmd.extend(["--browser-executable", browser_executable])
    if headful:
        cmd.append("--headful")

    meta = {
        "type": "reddit_watch",
        "subreddit": subreddit,
        "sort": sort,
        "time": time_window,
        "query": query,
        "policy": policy,
        "interval_seconds": interval_seconds,
        "limit": limit,
        "state_file": str(state_path),
    }
    if hydrate_mode:
        meta["hydrate_mode"] = hydrate_mode
    if min_age_minutes is not None:
        meta["min_age_minutes"] = min_age_minutes
    if hydrate_min_score is not None:
        meta["hydrate_min_score"] = hydrate_min_score
    if hydrate_min_comments is not None:
        meta["hydrate_min_comments"] = hydrate_min_comments
    if revisit_after_minutes:
        meta["revisit_after_minutes"] = revisit_after_minutes
    if browser_executable:
        meta["browser_executable"] = browser_executable
    if headful:
        meta["headful"] = True

    store = LeadStore()
    store.create_job(job_id, "reddit_watch", total=0, meta=meta)

    env = {**os.environ, "SUPLEX_JOB_ID": job_id}
    return _launch(job_id, cmd, env, log_path, meta=meta)


def launch_reddit_revisit_job(
    *,
    subreddit: str | None = None,
    policy: str | None = None,
    hydrate_mode: str | None = None,
    min_age_minutes: int | None = None,
    hydrate_min_score: int | None = None,
    hydrate_min_comments: int | None = None,
    revisit_after_minutes: str | None = None,
    limit: int = 25,
    browser_executable: str | None = None,
    headful: bool = False,
    job_id: str | None = None,
) -> dict:
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)
    cmd = [
        sys.executable,
        "-m",
        "suplex.reddit_watch",
        "--mode",
        "revisit",
        "--limit",
        str(limit),
    ]
    meta: dict[str, object] = {
        "type": "reddit_revisit",
        "limit": limit,
    }
    if policy:
        cmd.extend(["--policy", policy])
        meta["policy"] = policy
    if subreddit:
        cmd.extend(["--subreddit", subreddit])
        meta["subreddit"] = subreddit
    if hydrate_mode:
        cmd.extend(["--hydrate-mode", hydrate_mode])
        meta["hydrate_mode"] = hydrate_mode
    if min_age_minutes is not None:
        cmd.extend(["--min-age-minutes", str(min_age_minutes)])
        meta["min_age_minutes"] = min_age_minutes
    if hydrate_min_score is not None:
        cmd.extend(["--hydrate-min-score", str(hydrate_min_score)])
        meta["hydrate_min_score"] = hydrate_min_score
    if hydrate_min_comments is not None:
        cmd.extend(["--hydrate-min-comments", str(hydrate_min_comments)])
        meta["hydrate_min_comments"] = hydrate_min_comments
    if revisit_after_minutes:
        cmd.extend(["--revisit-after-minutes", revisit_after_minutes])
        meta["revisit_after_minutes"] = revisit_after_minutes
    if browser_executable:
        cmd.extend(["--browser-executable", browser_executable])
        meta["browser_executable"] = browser_executable
    if headful:
        cmd.append("--headful")
        meta["headful"] = True

    store = LeadStore()
    store.create_job(job_id, "reddit_revisit", total=0, meta=meta)
    env = {**os.environ, "SUPLEX_JOB_ID": job_id}
    return _launch(job_id, cmd, env, log_path, meta=meta)


def launch_reddit_hydrate_job(
    *,
    subreddit: str | None = None,
    post_ids: list[str] | None = None,
    limit: int = 25,
    comments_limit: int = 25,
    comment_sort: str = "best",
    expand_replies: bool = False,
    reply_pages_limit: int = 20,
    browser_executable: str | None = None,
    headful: bool = False,
    debug: bool = False,
    job_id: str | None = None,
) -> dict:
    job_id = job_id or _gen_id()
    log_path = _log_path(job_id)
    cmd = [
        sys.executable,
        "-m",
        "suplex.reddit_watch",
        "--mode",
        "hydrate",
        "--limit",
        str(limit),
        "--comments-limit",
        str(comments_limit),
        "--comment-sort",
        comment_sort,
        "--reply-pages-limit",
        str(reply_pages_limit),
    ]
    meta: dict[str, object] = {
        "type": "reddit_hydrate",
        "limit": limit,
        "comments_limit": comments_limit,
        "comment_sort": comment_sort,
        "reply_pages_limit": reply_pages_limit,
    }
    if subreddit:
        cmd.extend(["--subreddit", subreddit])
        meta["subreddit"] = subreddit
    for post_id in post_ids or []:
        cmd.extend(["--post-id", post_id])
    if post_ids:
        meta["post_ids"] = post_ids
    if expand_replies:
        cmd.append("--expand-replies")
        meta["expand_replies"] = True
    if browser_executable:
        cmd.extend(["--browser-executable", browser_executable])
        meta["browser_executable"] = browser_executable
    if headful:
        cmd.append("--headful")
        meta["headful"] = True
    if debug:
        cmd.append("--debug")
        meta["debug"] = True

    store = LeadStore()
    store.create_job(job_id, "reddit_hydrate", total=0, meta=meta)
    env = {**os.environ, "SUPLEX_JOB_ID": job_id}
    return _launch(job_id, cmd, env, log_path, meta=meta)


def launch_batch_job(specs: list[dict], *, max_parallel: int = 3) -> dict:
    """Launch a batch of scrape/enrich jobs with concurrency control.

    Each spec: {"type": "scrape_maps"|"scrape_linkedin"|"enrich", "query": ..., "location": ..., ...}
    """
    if not specs:
        return {"error": "A batch needs at least one job"}
    unknown = sorted({str(spec.get("type")) for spec in specs} - BATCH_CHILD_TYPES)
    if unknown:
        return {
            "error": f"Unknown job type(s): {', '.join(unknown)}. "
                     f"Valid types: {', '.join(sorted(BATCH_CHILD_TYPES))}",
        }

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
    status = _reconciled_batch_status(store, batch_id)
    if not status:
        return {"error": f"Batch {batch_id} not found"}

    if status["status"] == "stopped":
        return {**status, "newly_launched": 0}

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
    terminal = _terminal_count(status)
    if terminal == status["total_jobs"]:
        final_status = _final_batch_status(status)
        store.finish_job(batch_id, final_status)

    return {**status, "newly_launched": len(newly_launched)}


def get_batch_status(batch_id: str) -> dict:
    """Get batch status with aggregated progress."""
    store = LeadStore()
    status = _reconciled_batch_status(store, batch_id)
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
        if meta.get("scraper") in ("playwright", "apify"):
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
                     (utcnow().isoformat(), job_id))

    if job_type in ("scrape_maps", "scrape_linkedin"):
        source = "maps" if job_type == "scrape_maps" else "linkedin"
        query = meta.get("query", "")
        location = meta.get("location", "")
        max_results = meta.get("max_results", 0)
        extra = _meta_to_extra_args(job_type, meta)
        if max_results > 0:
            extra.extend(["-n", str(max_results)])
        launch_scrape_job(source, query, location, extra_args=extra, job_id=job_id)
    elif job_type == "enrich":
        launch_enrich_job(
            limit=meta.get("limit", 100),
            linkedin_posts=meta.get("linkedin_posts", 1),
            job_id=job_id,
        )
    elif job_type == "reddit_watch":
        launch_reddit_watch_job(
            meta.get("subreddit", ""),
            sort=meta.get("sort", "new"),
            time_window=meta.get("time", "month"),
            query=meta.get("query", ""),
            policy=meta.get("policy", "balanced"),
            hydrate_mode=meta.get("hydrate_mode"),
            min_age_minutes=meta.get("min_age_minutes"),
            hydrate_min_score=meta.get("hydrate_min_score"),
            hydrate_min_comments=meta.get("hydrate_min_comments"),
            revisit_after_minutes=meta.get("revisit_after_minutes"),
            interval_seconds=int(meta.get("interval_seconds", 30)),
            limit=int(meta.get("limit", 25)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            job_id=job_id,
        )
    elif job_type == "reddit_revisit":
        launch_reddit_revisit_job(
            subreddit=meta.get("subreddit"),
            policy=meta.get("policy"),
            hydrate_mode=meta.get("hydrate_mode"),
            min_age_minutes=meta.get("min_age_minutes"),
            hydrate_min_score=meta.get("hydrate_min_score"),
            hydrate_min_comments=meta.get("hydrate_min_comments"),
            revisit_after_minutes=meta.get("revisit_after_minutes"),
            limit=int(meta.get("limit", 25)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            job_id=job_id,
        )
    elif job_type == "reddit_hydrate":
        launch_reddit_hydrate_job(
            subreddit=meta.get("subreddit"),
            post_ids=list(meta.get("post_ids", []) or []),
            limit=int(meta.get("limit", 25)),
            comments_limit=int(meta.get("comments_limit", 25)),
            comment_sort=meta.get("comment_sort", "best"),
            expand_replies=bool(meta.get("expand_replies", False)),
            reply_pages_limit=int(meta.get("reply_pages_limit", 20)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            debug=bool(meta.get("debug", False)),
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
    job = _reconcile_job(store, job)

    if job["status"] == "running":
        return {"error": "Job is already running", "job_id": job_id}

    if job["status"] == "done":
        counts = store.get_item_task_counts(job_id)
        failed = counts.get("failed", 0)
        if failed == 0:
            return {"error": "Job already completed with no failures", "job_id": job_id}

    # Reset failed tasks to pending so they get retried.
    reset = store.retry_failed_tasks(job_id)
    store.reset_processing_tasks(job_id)

    meta = job["meta"]
    job_type = job["type"]

    if job_type == "scrape":
        source = meta.get("source", "maps")
        query = meta.get("query", "")
        location = meta.get("location", "")
        extra = list(meta.get("extra_args", []))
        # Apply scraper override — only valid for Google Maps, not LinkedIn.
        if scraper_override and source == "maps":
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
            # Persist updated args so future resumes use the new scraper.
            meta["extra_args"] = extra
            _update_job_meta(store, job_id, meta)
        elif scraper_override and source == "linkedin":
            logger.warning("Scraper override ignored — LinkedIn always uses Apify")
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
            meta["scraper"] = scraper_override
            _update_job_meta(store, job_id, meta)
        elif scraper_override and job_type == "scrape_linkedin":
            logger.warning("Scraper override ignored — LinkedIn always uses Apify")
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
    elif job_type == "reddit_watch":
        launch_reddit_watch_job(
            meta.get("subreddit", ""),
            sort=meta.get("sort", "new"),
            time_window=meta.get("time", "month"),
            query=meta.get("query", ""),
            policy=meta.get("policy", "balanced"),
            hydrate_mode=meta.get("hydrate_mode"),
            min_age_minutes=meta.get("min_age_minutes"),
            hydrate_min_score=meta.get("hydrate_min_score"),
            hydrate_min_comments=meta.get("hydrate_min_comments"),
            revisit_after_minutes=meta.get("revisit_after_minutes"),
            interval_seconds=int(meta.get("interval_seconds", 30)),
            limit=int(meta.get("limit", 25)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            job_id=job_id,
        )
    elif job_type == "reddit_revisit":
        launch_reddit_revisit_job(
            subreddit=meta.get("subreddit"),
            policy=meta.get("policy"),
            hydrate_mode=meta.get("hydrate_mode"),
            min_age_minutes=meta.get("min_age_minutes"),
            hydrate_min_score=meta.get("hydrate_min_score"),
            hydrate_min_comments=meta.get("hydrate_min_comments"),
            revisit_after_minutes=meta.get("revisit_after_minutes"),
            limit=int(meta.get("limit", 25)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            job_id=job_id,
        )
    elif job_type == "reddit_hydrate":
        launch_reddit_hydrate_job(
            subreddit=meta.get("subreddit"),
            post_ids=list(meta.get("post_ids", []) or []),
            limit=int(meta.get("limit", 25)),
            comments_limit=int(meta.get("comments_limit", 25)),
            comment_sort=meta.get("comment_sort", "best"),
            expand_replies=bool(meta.get("expand_replies", False)),
            reply_pages_limit=int(meta.get("reply_pages_limit", 20)),
            browser_executable=meta.get("browser_executable"),
            headful=bool(meta.get("headful", False)),
            debug=bool(meta.get("debug", False)),
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
    job = _reconcile_job(store, job)

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

    runtime = job["meta"].get("runtime", {})
    if runtime.get("pid") is not None:
        result["pid"] = runtime["pid"]
    if runtime.get("pgid") is not None:
        result["pgid"] = runtime["pgid"]
    if runtime.get("log"):
        result["log"] = runtime["log"]
    if job["meta"].get("watchdog", {}).get("pid") is not None:
        result["watchdog_pid"] = job["meta"]["watchdog"]["pid"]

    result["meta"] = job["meta"]
    return result


def list_jobs(status: str | None = None) -> list[dict]:
    """List recent jobs."""
    store = LeadStore()
    for job in store.list_jobs(status="running"):
        _reconcile_job(store, job)
    return store.list_jobs(status=status)


def stop_job(job_id: str, *, force: bool = False, store: LeadStore | None = None) -> dict:
    """Stop a running or pending background job.

    For batch jobs, this also stops the watchdog and any running children, and marks pending
    children as stopped so the batch cannot relaunch work behind the user's back.
    """
    store = store or LeadStore()
    job = store.get_job(job_id)
    if not job:
        return {"error": f"Job {job_id} not found"}

    if job["type"] == "batch":
        return _stop_batch_job(store, job, force=force)

    return _stop_single_job(store, job, force=force, reason="stopped_by_user")


def _spawn_watchdog(batch_id: str, max_parallel: int) -> None:
    """Spawn a detached watchdog process that auto-advances the batch."""
    cmd = [
        sys.executable, "-c",
        f"from suplex.jobs import _watchdog_loop; _watchdog_loop('{batch_id}', {max_parallel})",
    ]
    log_path = _log_path(f"{batch_id}_watchdog")
    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    store = LeadStore()
    batch = store.get_job(batch_id)
    meta = batch["meta"] if batch else {}
    meta["watchdog"] = _runtime_meta_from_process(proc, log_path)
    _update_job_meta(store, batch_id, meta)


def _watchdog_loop(batch_id: str, max_parallel: int, poll_interval: int = 15) -> None:
    """Check every poll_interval seconds and launch next jobs when slots open.

    Runs as a detached subprocess. Exits when batch is complete.
    """
    store = LeadStore()

    while True:
        status = _reconciled_batch_status(store, batch_id)
        if not status:
            break

        if status["status"] == "stopped":
            print(f"Batch {batch_id} stopped")
            break

        terminal = _terminal_count(status)
        total = status["total_jobs"]

        # Batch complete?
        if terminal >= total:
            final_status = _final_batch_status(status)
            store.finish_job(batch_id, final_status)
            print(
                f"Batch {batch_id} complete: {final_status} "
                f"({status['done']} done, {status.get('done_with_errors', 0)} with errors, "
                f"{status['failed']} failed, {status.get('stopped', 0)} stopped)"
            )
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


def _update_job_meta(store: LeadStore, job_id: str, meta: dict) -> None:
    """Persist updated meta to the job row (e.g., after scraper override on resume)."""
    store.update_job_meta(job_id, meta)


def _terminal_count(status: dict) -> int:
    """How many children of a batch have finished, in any way."""
    return (
        status["done"] + status.get("done_with_errors", 0)
        + status["failed"] + status.get("stopped", 0)
    )


def _final_batch_status(status: dict) -> str:
    if status.get("stopped", 0) > 0:
        return "stopped"
    if status["failed"] > 0 or status.get("done_with_errors", 0) > 0:
        return "done_with_errors"
    return "done"


def _runtime_meta_from_process(proc: subprocess.Popen, log_path: Path) -> dict:
    runtime = {
        "pid": proc.pid,
        "log": str(log_path),
    }
    try:
        runtime["pgid"] = os.getpgid(proc.pid)
    except ProcessLookupError:
        runtime["pgid"] = proc.pid
    return runtime


def _normalize_runtime_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, str)) or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _terminate_runtime(runtime: dict | None, *, force: bool = False) -> dict:
    runtime = runtime or {}
    pid = _normalize_runtime_value(runtime.get("pid"))
    pgid = _normalize_runtime_value(runtime.get("pgid")) or pid
    signal_name = "SIGKILL" if force else "SIGTERM"

    if pid is None and pgid is None:
        return {
            "pid": None,
            "pgid": None,
            "signal": signal_name,
            "process_found": False,
            "signal_sent": False,
            "escalated_to_sigkill": False,
        }

    process_found = _pid_exists(pid)
    signal_sent = False
    escalated = False

    if process_found:
        _send_signal(pid=pid, pgid=pgid, sig=signal.SIGKILL if force else signal.SIGTERM)
        signal_sent = True
        if not force and pid is not None and not _wait_for_exit(pid, timeout_seconds=5.0):
            _send_signal(pid=pid, pgid=pgid, sig=signal.SIGKILL)
            escalated = True

    return {
        "pid": pid,
        "pgid": pgid,
        "signal": signal_name,
        "process_found": process_found,
        "signal_sent": signal_sent,
        "escalated_to_sigkill": escalated,
        "log": runtime.get("log"),
    }


def _send_signal(*, pid: int | None, pgid: int | None, sig: int) -> None:
    if pgid is not None and hasattr(os, "killpg"):
        try:
            os.killpg(pgid, sig)
            return
        except ProcessLookupError:
            return
    if pid is not None:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def _pid_exists(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _wait_for_exit(pid: int, *, timeout_seconds: float) -> bool:
    try:
        psutil.Process(pid).wait(timeout=timeout_seconds)
        return True
    except psutil.NoSuchProcess:
        return True
    except psutil.TimeoutExpired:
        return False


def _reconcile_job(store: LeadStore, job: dict) -> dict:
    if job["status"] != "running":
        return job
    pid = _normalize_runtime_value(job.get("meta", {}).get("runtime", {}).get("pid"))
    if pid is None or _pid_exists(pid):
        return job
    store.reset_processing_tasks(job["id"])
    store.finish_job(
        job["id"], "failed",
        error=f"Process {pid} exited without reporting a result. Run `suplex resume {job['id']}` to continue.",
    )
    return store.get_job(job["id"]) or job


def _reconciled_batch_status(store: LeadStore, batch_id: str) -> dict | None:
    status = store.get_batch_status(batch_id)
    if not status:
        return None
    dead = [c for c in status["children"] if _reconcile_job(store, c)["status"] != c["status"]]
    return store.get_batch_status(batch_id) if dead else status


def _stop_single_job(
    store: LeadStore,
    job: dict,
    *,
    force: bool,
    reason: str,
) -> dict:
    job_id = job["id"]
    if job["status"] in TERMINAL_JOB_STATUSES:
        return {
            "job_id": job_id,
            "status": job["status"],
            "already_terminal": True,
        }

    runtime_result = _terminate_runtime(job.get("meta", {}).get("runtime"), force=force)
    reset = store.reset_processing_tasks(job_id)
    store.finish_job(job_id, "stopped", error=reason)

    result = {
        "job_id": job_id,
        "status": "stopped",
        "type": job["type"],
        "tasks_reset": reset,
        **runtime_result,
    }
    if job.get("parent_id"):
        result["parent_id"] = job["parent_id"]
    return result


def _stop_batch_job(store: LeadStore, job: dict, *, force: bool) -> dict:
    batch_id = job["id"]
    if job["status"] in TERMINAL_JOB_STATUSES:
        return {
            "job_id": batch_id,
            "status": job["status"],
            "type": "batch",
            "already_terminal": True,
        }

    status = store.get_batch_status(batch_id)
    if not status:
        return {"error": f"Batch {batch_id} not found"}

    watchdog_result = _terminate_runtime(job.get("meta", {}).get("watchdog"), force=force)
    stopped_children: list[dict] = []
    pending_children_stopped: list[str] = []

    for child in status["children"]:
        if child["status"] == "running":
            stopped_children.append(
                _stop_single_job(store, child, force=force, reason="stopped_by_batch"),
            )
        elif child["status"] == "pending":
            store.finish_job(child["id"], "stopped", error="stopped_by_batch")
            pending_children_stopped.append(child["id"])

    store.finish_job(batch_id, "stopped", error="stopped_by_user")

    refreshed = store.get_batch_status(batch_id) or status
    return {
        "job_id": batch_id,
        "status": "stopped",
        "type": "batch",
        "watchdog": watchdog_result,
        "running_children_stopped": len(stopped_children),
        "pending_children_stopped": len(pending_children_stopped),
        "stopped_children": stopped_children,
        "pending_child_ids": pending_children_stopped,
        "summary": {
            "done": refreshed["done"],
            "done_with_errors": refreshed.get("done_with_errors", 0),
            "failed": refreshed["failed"],
            "stopped": refreshed.get("stopped", 0),
            "pending": refreshed["pending"],
            "running": refreshed["running"],
        },
    }


def _suplex_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, "-m", "suplex"]


def _gen_id() -> str:
    return uuid.uuid4().hex[:12]


def _log_path(job_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{job_id}.log"


def _state_path(job_id: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{job_id}.state.json"


def _launch(job_id: str, cmd: list[str], env: dict, log_path: Path, meta: dict) -> dict:
    store = LeadStore()
    try:
        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )
    except OSError as exc:
        error = f"Could not start job process: {exc}"
        store.finish_job(job_id, "failed", error=error)
        return {"job_id": job_id, "status": "failed", "error": error, **meta}

    runtime = _runtime_meta_from_process(proc, log_path)
    job = store.get_job(job_id)
    job_meta = dict(job["meta"]) if job else dict(meta)
    job_meta["runtime"] = runtime
    _update_job_meta(store, job_id, job_meta)

    logger.info("Launched job %s (pid=%d): %s", job_id, proc.pid, " ".join(cmd))

    return {
        "job_id": job_id,
        "pid": runtime["pid"],
        "pgid": runtime.get("pgid"),
        "log": runtime["log"],
        "status": "launched",
        **meta,
    }
