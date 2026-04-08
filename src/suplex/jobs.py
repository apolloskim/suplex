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
) -> dict:
    """Launch a scrape job in the background. Returns job info immediately."""
    job_id = _gen_id()
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
) -> dict:
    """Launch an enrichment job in the background."""
    job_id = _gen_id()
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
