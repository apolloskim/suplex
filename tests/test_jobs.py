from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from suplex.jobs import stop_job
from suplex.store.db import LeadStore


SLEEP_BIN = shutil.which("sleep") or "/bin/sleep"


def _runtime_meta(pid: int, log_name: str) -> dict:
    return {
        "pid": pid,
        "pgid": os.getpgid(pid),
        "log": f"/tmp/{log_name}.log",
    }


def test_stop_job_marks_single_job_stopped_and_resets_processing_tasks() -> None:
    with TemporaryDirectory() as tmpdir:
        store = LeadStore(db_path=Path(tmpdir) / "suplex.db")
        job_id = "job_stop_single"
        store.create_job(job_id, "scrape", total=1, meta={"source": "maps"})
        store.seed_item_tasks(job_id, ["maps:texas:dentists"])
        assert store.claim_item_task(job_id) == "maps:texas:dentists"

        proc = subprocess.Popen([SLEEP_BIN, "30"], start_new_session=True)
        try:
            job = store.get_job(job_id)
            assert job is not None
            meta = dict(job["meta"])
            meta["runtime"] = _runtime_meta(proc.pid, job_id)
            store.update_job_meta(job_id, meta)

            result = stop_job(job_id, store=store)

            assert result["status"] == "stopped"
            assert result["signal_sent"] is True
            assert result["process_found"] is True
            assert result["tasks_reset"] == 1
            assert store.get_job(job_id)["status"] == "stopped"
            assert store.get_item_task_counts(job_id)["pending"] == 1
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


def test_stop_job_stops_batch_watchdog_running_child_and_pending_child() -> None:
    with TemporaryDirectory() as tmpdir:
        store = LeadStore(db_path=Path(tmpdir) / "suplex.db")
        batch_id = "batch_stop"
        child_running_id = "child_running"
        child_pending_id = "child_pending"
        store.create_batch_job(
            batch_id,
            [
                {"job_id": child_running_id, "type": "scrape_maps", "meta": {"query": "dentist", "location": "Texas"}},
                {"job_id": child_pending_id, "type": "scrape_maps", "meta": {"query": "dentist", "location": "Ohio"}},
            ],
            meta={"total_specs": 2},
        )
        with store._conn() as conn:
            conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (child_running_id,))

        store.seed_item_tasks(child_running_id, ["maps:texas:dentist"])
        assert store.claim_item_task(child_running_id) == "maps:texas:dentist"

        child_proc = subprocess.Popen([SLEEP_BIN, "30"], start_new_session=True)
        watchdog_proc = subprocess.Popen([SLEEP_BIN, "30"], start_new_session=True)
        try:
            running_child = store.get_job(child_running_id)
            assert running_child is not None
            running_meta = dict(running_child["meta"])
            running_meta["runtime"] = _runtime_meta(child_proc.pid, child_running_id)
            store.update_job_meta(child_running_id, running_meta)

            batch = store.get_job(batch_id)
            assert batch is not None
            batch_meta = dict(batch["meta"])
            batch_meta["watchdog"] = _runtime_meta(watchdog_proc.pid, f"{batch_id}_watchdog")
            store.update_job_meta(batch_id, batch_meta)

            result = stop_job(batch_id, store=store)

            assert result["status"] == "stopped"
            assert result["watchdog"]["signal_sent"] is True
            assert result["running_children_stopped"] == 1
            assert result["pending_children_stopped"] == 1
            assert result["summary"]["stopped"] == 2
            assert store.get_job(batch_id)["status"] == "stopped"
            assert store.get_job(child_running_id)["status"] == "stopped"
            assert store.get_job(child_pending_id)["status"] == "stopped"
            assert store.get_item_task_counts(child_running_id)["pending"] == 1
            child_proc.wait(timeout=5)
            watchdog_proc.wait(timeout=5)
        finally:
            if child_proc.poll() is None:
                child_proc.kill()
                child_proc.wait(timeout=5)
            if watchdog_proc.poll() is None:
                watchdog_proc.kill()
                watchdog_proc.wait(timeout=5)
