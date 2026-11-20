from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from suplex.clock import utcnow
from suplex.models import Lead, LeadSource, VerificationStatus

DEFAULT_DB_PATH = Path.home() / ".config" / "suplex" / "suplex.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    company TEXT,
    title TEXT,
    phone TEXT,
    location TEXT,
    linkedin_url TEXT,
    google_maps_url TEXT,
    website TEXT,
    source TEXT NOT NULL,
    raw_data TEXT DEFAULT '{}',
    scraped_at TEXT NOT NULL,
    verification_status TEXT DEFAULT 'pending',
    verified_at TEXT,
    enriched_at TEXT,
    enrichment_data TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_verification ON leads(verification_status);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company);

CREATE TABLE IF NOT EXISTS cities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    state TEXT NOT NULL,
    country_code TEXT NOT NULL DEFAULT 'US',
    country_name TEXT NOT NULL DEFAULT 'United States',
    lat REAL NOT NULL,
    lng REAL NOT NULL,
    population INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cities_state ON cities(state);
CREATE INDEX IF NOT EXISTS idx_cities_country ON cities(country_code);
CREATE INDEX IF NOT EXISTS idx_cities_pop ON cities(population DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cities_name_state_country ON cities(name, state, country_code);

CREATE TABLE IF NOT EXISTS linkedin_locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL UNIQUE,
    suggestions TEXT NOT NULL DEFAULT '[]',
    scraped_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_linkedin_loc_query ON linkedin_locations(query);

CREATE TABLE IF NOT EXISTS linkedin_geo_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lookup_key TEXT NOT NULL,
    autocomplete TEXT NOT NULL,
    UNIQUE(lookup_key, autocomplete)
);

CREATE INDEX IF NOT EXISTS idx_linkedin_geo_key ON linkedin_geo_map(lookup_key);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    total INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    leads_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    meta TEXT NOT NULL DEFAULT '{}',
    parent_id TEXT REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_id);

CREATE TABLE IF NOT EXISTS item_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error TEXT,
    UNIQUE(job_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_item_tasks_job ON item_tasks(job_id, status);

CREATE TABLE IF NOT EXISTS sender_profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    website TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Outreach: mailboxes, campaigns, sends, suppression.

CREATE TABLE IF NOT EXISTS sender_mailboxes (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    gws_config_dir TEXT NOT NULL,
    daily_limit INTEGER NOT NULL DEFAULT 10,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    filter_json TEXT NOT NULL DEFAULT '{}',
    cadence_json TEXT NOT NULL DEFAULT '[{"step":1,"delay_days":0},{"step":2,"delay_days":3},{"step":3,"delay_days":7}]',
    mailbox_id TEXT REFERENCES sender_mailboxes(id),
    physical_address TEXT NOT NULL DEFAULT '',
    playbook_override TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outreach (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    lead_id INTEGER NOT NULL REFERENCES leads(id),
    mailbox_id TEXT REFERENCES sender_mailboxes(id),
    step INTEGER NOT NULL DEFAULT 1,
    subject TEXT,
    body TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    gmail_thread_id TEXT,
    gmail_message_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    drafted_at TEXT,
    approved_at TEXT,
    sent_at TEXT,
    replied_at TEXT,
    UNIQUE(campaign_id, lead_id, step)
);

CREATE INDEX IF NOT EXISTS idx_outreach_campaign ON outreach(campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_outreach_lead ON outreach(lead_id);
CREATE INDEX IF NOT EXISTS idx_outreach_mailbox_sent ON outreach(mailbox_id, sent_at);

CREATE TABLE IF NOT EXISTS suppression_list (
    email TEXT PRIMARY KEY,
    reason TEXT NOT NULL DEFAULT 'opt_out',
    source TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_posts (
    post_id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    author TEXT,
    created_timestamp TEXT,
    selftext TEXT NOT NULL DEFAULT '',
    domain TEXT,
    flair TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    matched_query TEXT NOT NULL DEFAULT '',
    matched INTEGER NOT NULL DEFAULT 0,
    last_sort TEXT NOT NULL DEFAULT 'new',
    last_time_window TEXT NOT NULL DEFAULT 'month',
    last_score INTEGER NOT NULL DEFAULT 0,
    last_num_comments INTEGER NOT NULL DEFAULT 0,
    signal_status TEXT NOT NULL DEFAULT 'seen',
    hydration_status TEXT NOT NULL DEFAULT 'pending',
    next_revisit_at TEXT,
    revisit_count INTEGER NOT NULL DEFAULT 0,
    last_hydrated_at TEXT,
    last_hydration_error TEXT,
    policy_json TEXT NOT NULL DEFAULT '{}',
    post_payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reddit_posts_subreddit ON reddit_posts(subreddit, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_revisit ON reddit_posts(next_revisit_at, signal_status);
CREATE INDEX IF NOT EXISTS idx_reddit_posts_hydration ON reddit_posts(hydration_status, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS reddit_post_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL REFERENCES reddit_posts(post_id),
    seen_at TEXT NOT NULL,
    sort TEXT NOT NULL DEFAULT 'new',
    time_window TEXT NOT NULL DEFAULT 'month',
    score INTEGER NOT NULL DEFAULT 0,
    num_comments INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_reddit_snapshots_post_seen ON reddit_post_snapshots(post_id, seen_at DESC);
"""


class LeadStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def insert_lead(self, lead: Lead) -> int:
        """Insert a lead with deduplication.

        Dedup keys (checked in order):
        1. linkedin_url (for LinkedIn leads)
        2. google_maps_url (for Google Maps leads)
        3. email + source (for any source with email)
        4. name + location + source (fallback)
        """
        with self._conn() as conn:
            # Check for existing lead.
            existing_id = self._find_duplicate(conn, lead)
            if existing_id:
                # Update existing lead with new data (merge).
                conn.execute(
                    """UPDATE leads SET
                        name=COALESCE(?, name), email=COALESCE(?, email),
                        company=COALESCE(?, company), title=COALESCE(?, title),
                        phone=COALESCE(?, phone), location=COALESCE(?, location),
                        linkedin_url=COALESCE(?, linkedin_url),
                        google_maps_url=COALESCE(?, google_maps_url),
                        website=COALESCE(?, website),
                        raw_data=?, scraped_at=?
                    WHERE id = ?""",
                    (
                        lead.name, lead.email, lead.company, lead.title,
                        lead.phone, lead.location, lead.linkedin_url,
                        lead.google_maps_url, lead.website,
                        json.dumps(lead.raw_data), lead.scraped_at.isoformat(),
                        existing_id,
                    ),
                )
                return existing_id

            cur = conn.execute(
                """INSERT INTO leads (name, email, company, title, phone, location,
                    linkedin_url, google_maps_url, website, source, raw_data,
                    scraped_at, verification_status, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    lead.name, lead.email, lead.company, lead.title,
                    lead.phone, lead.location, lead.linkedin_url,
                    lead.google_maps_url, lead.website, lead.source.value,
                    json.dumps(lead.raw_data), lead.scraped_at.isoformat(),
                    lead.verification_status.value,
                    lead.verified_at.isoformat() if lead.verified_at else None,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

    @staticmethod
    def _find_duplicate(conn: sqlite3.Connection, lead: Lead) -> int | None:
        """Find an existing lead that matches this one."""
        # 1. LinkedIn URL (strongest identifier).
        if lead.linkedin_url:
            row = conn.execute(
                "SELECT id FROM leads WHERE linkedin_url = ?", (lead.linkedin_url,)
            ).fetchone()
            if row:
                return row["id"]

        # 2. Google Maps URL.
        if lead.google_maps_url:
            row = conn.execute(
                "SELECT id FROM leads WHERE google_maps_url = ?", (lead.google_maps_url,)
            ).fetchone()
            if row:
                return row["id"]

        # 3. Email + source.
        if lead.email:
            row = conn.execute(
                "SELECT id FROM leads WHERE email = ? AND source = ?",
                (lead.email, lead.source.value),
            ).fetchone()
            if row:
                return row["id"]

        # 4. Name + location + source (fuzzy match for businesses).
        if lead.name and lead.location:
            row = conn.execute(
                "SELECT id FROM leads WHERE name = ? AND location = ? AND source = ?",
                (lead.name, lead.location, lead.source.value),
            ).fetchone()
            if row:
                return row["id"]

        return None

    def insert_leads(self, leads: list[Lead]) -> int:
        count = 0
        for lead in leads:
            self.insert_lead(lead)
            count += 1
        return count

    def get_leads(
        self,
        *,
        source: LeadSource | None = None,
        verification_status: VerificationStatus | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> list[Lead]:
        clauses: list[str] = []
        params: list[str] = []
        if source:
            clauses.append("source = ?")
            params.append(source.value)
        if verification_status:
            clauses.append("verification_status = ?")
            params.append(verification_status.value)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM leads {where} ORDER BY scraped_at DESC LIMIT ? OFFSET ?"
        params.extend([str(limit), str(offset)])

        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_lead(row) for row in rows]

    def get_lead(self, lead_id: int) -> Lead | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
            return self._row_to_lead(row) if row else None

    def get_unverified_emails(self, limit: int = 100) -> list[Lead]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM leads
                WHERE email IS NOT NULL AND verification_status = 'pending'
                ORDER BY scraped_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._row_to_lead(row) for row in rows]

    def update_verification(
        self, lead_id: int, status: VerificationStatus
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET verification_status = ?, verified_at = ? WHERE id = ?",
                (status.value, utcnow().isoformat(), lead_id),
            )

    def get_unenriched(self, limit: int = 100) -> list[Lead]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM leads WHERE enriched_at IS NULL ORDER BY scraped_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._row_to_lead(row) for row in rows]

    def update_enrichment(self, lead_id: int, enrichment_data: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET enriched_at = ?, enrichment_data = ? WHERE id = ?",
                (utcnow().isoformat(), json.dumps(enrichment_data, default=str), lead_id),
            )

    def update_email(self, lead_id: int, email: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE leads SET email = ? WHERE id = ?",
                (email, lead_id),
            )

    def count(
        self,
        *,
        source: LeadSource | None = None,
        verification_status: VerificationStatus | None = None,
    ) -> int:
        clauses: list[str] = []
        params: list[str] = []
        if source:
            clauses.append("source = ?")
            params.append(source.value)
        if verification_status:
            clauses.append("verification_status = ?")
            params.append(verification_status.value)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._conn() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM leads {where}", params).fetchone()
            return row[0]

    # ------------------------------------------------------------------
    # Sender profile
    # ------------------------------------------------------------------

    def save_profile(self, website: str, data: dict) -> None:
        now = utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sender_profile (id, website, data, created_at, updated_at)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    website=excluded.website, data=excluded.data, updated_at=excluded.updated_at
                """,
                (website, json.dumps(data, default=str), now, now),
            )

    def get_profile(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sender_profile WHERE id = 1").fetchone()
            if row:
                return {
                    "website": row["website"],
                    **json.loads(row["data"]),
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
        return None

    # ------------------------------------------------------------------
    # Cities (geo tile planning)
    # ------------------------------------------------------------------

    def cities_seeded(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM cities").fetchone()
            return row[0] > 0

    def seed_cities(self, cities: list[dict]) -> int:
        """Bulk insert cities. Each dict: {name, state, lat, lng, pop, country_code?, country_name?}."""
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO cities (name, state, country_code, country_name, lat, lng, population)
                VALUES (:name, :state, :country_code, :country_name, :lat, :lng, :pop)
                """,
                cities,
            )
            return len(cities)

    def get_cities(
        self,
        state: str,
        *,
        country_code: str = "US",
        min_population: int = 0,
        limit: int = 9999,
    ) -> list[dict]:
        """Get top cities in a state/region by population."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT name, state, country_code, lat, lng, population
                FROM cities
                WHERE state = ? AND country_code = ? AND population >= ?
                ORDER BY population DESC
                LIMIT ?
                """,
                (state, country_code, min_population, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_cities_by_country(
        self,
        country_code: str,
        *,
        min_population: int = 0,
        limit: int = 9999,
    ) -> list[dict]:
        """Get all cities in a country by population."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT name, state, country_code, lat, lng, population
                FROM cities
                WHERE country_code = ? AND population >= ?
                ORDER BY population DESC
                LIMIT ?
                """,
                (country_code.upper(), min_population, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_regions(self, country_code: str = "US") -> list[str]:
        """Get all unique state/region names for a country."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT state FROM cities WHERE country_code = ? ORDER BY state",
                (country_code.upper(),),
            ).fetchall()
            return [row["state"] for row in rows]

    def get_all_countries(self) -> list[dict]:
        """Get all countries with city counts."""
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT country_code, country_name, COUNT(*) as city_count
                FROM cities GROUP BY country_code
                ORDER BY city_count DESC
                """,
            ).fetchall()
            return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # LinkedIn locations
    # ------------------------------------------------------------------

    def linkedin_locations_seeded(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM linkedin_locations").fetchone()
            return row[0] > 0

    def seed_linkedin_locations(self, records: list[dict]) -> int:
        """Bulk insert. Each dict: {query, suggestions (list[str]), scraped_at}."""
        with self._conn() as conn:
            conn.executemany(
                """
                INSERT OR IGNORE INTO linkedin_locations (query, suggestions, scraped_at)
                VALUES (:query, :suggestions, :scraped_at)
                """,
                records,
            )
            return len(records)

    def get_linkedin_location(self, query: str) -> list[str] | None:
        """Get cached LinkedIn autocomplete suggestions for a query. None if not cached."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT suggestions FROM linkedin_locations WHERE query = ?",
                (query,),
            ).fetchone()
            if row:
                return json.loads(row["suggestions"])
        return None

    def save_linkedin_location(self, query: str, suggestions: list[str]) -> None:
        """Cache a single LinkedIn location autocomplete result."""
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO linkedin_locations (query, suggestions, scraped_at)
                VALUES (?, ?, ?)
                """,
                (query, json.dumps(suggestions), utcnow().isoformat()),
            )

    # ------------------------------------------------------------------
    # LinkedIn geo map
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    def create_job(self, job_id: str, job_type: str, total: int, meta: dict | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO jobs (id, type, status, total, completed, leads_count, started_at, meta)
                VALUES (?, ?, 'running', ?, 0, 0, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status='running', total=CASE WHEN excluded.total > 0 THEN excluded.total ELSE jobs.total END,
                    started_at=excluded.started_at, finished_at=NULL, error=NULL
                """,
                (job_id, job_type, total, utcnow().isoformat(), json.dumps(meta or {})),
            )

    def update_job_progress(self, job_id: str, completed: int, leads_count: int) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET completed = ?, leads_count = ? WHERE id = ?",
                (completed, leads_count, job_id),
            )

    def finish_job(self, job_id: str, status: str = "done", error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, finished_at = ?, error = ? WHERE id = ?",
                (status, utcnow().isoformat(), error, job_id),
            )

    def update_job_meta(self, job_id: str, meta: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET meta = ? WHERE id = ?",
                (json.dumps(meta), job_id),
            )

    def get_job(self, job_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row:
                d = dict(row)
                d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
                return d
        return None

    def list_jobs(self, status: str | None = None, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY started_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY started_at DESC LIMIT ?", (limit,)
                ).fetchall()
            results = []
            for row in rows:
                d = dict(row)
                d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
                results.append(d)
            return results

    def create_batch_job(self, batch_id: str, child_specs: list[dict], meta: dict | None = None) -> None:
        """Create a parent batch job with pending child jobs."""
        now = utcnow().isoformat()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO jobs (id, type, status, total, started_at, meta) VALUES (?, 'batch', 'running', ?, ?, ?)",
                (batch_id, len(child_specs), now, json.dumps(meta or {})),
            )
            for spec in child_specs:
                conn.execute(
                    "INSERT INTO jobs (id, type, status, total, started_at, meta, parent_id) VALUES (?, ?, 'pending', 0, ?, ?, ?)",
                    (spec["job_id"], spec["type"], now, json.dumps(spec.get("meta", {})), batch_id),
                )

    def get_batch_status(self, batch_id: str) -> dict | None:
        """Get aggregated status of a batch and its children."""
        with self._conn() as conn:
            parent = conn.execute("SELECT * FROM jobs WHERE id = ?", (batch_id,)).fetchone()
            if not parent:
                return None
            children = conn.execute(
                "SELECT * FROM jobs WHERE parent_id = ? ORDER BY started_at", (batch_id,)
            ).fetchall()

        child_list = []
        for c in children:
            d = dict(c)
            d["meta"] = json.loads(d["meta"]) if d["meta"] else {}
            child_list.append(d)

        return {
            "batch_id": batch_id,
            "status": dict(parent)["status"],
            "total_jobs": len(children),
            "done": sum(1 for c in children if c["status"] == "done"),
            "done_with_errors": sum(1 for c in children if c["status"] == "done_with_errors"),
            "running": sum(1 for c in children if c["status"] == "running"),
            "failed": sum(1 for c in children if c["status"] == "failed"),
            "pending": sum(1 for c in children if c["status"] == "pending"),
            "stopped": sum(1 for c in children if c["status"] == "stopped"),
            "total_leads": sum(c["leads_count"] for c in children),
            "children": child_list,
        }

    def get_pending_children(self, batch_id: str, limit: int = 1) -> list[dict]:
        """Get next pending child jobs to launch."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE parent_id = ? AND status = 'pending' LIMIT ?",
                (batch_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def count_running_children(self, batch_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE parent_id = ? AND status = 'running'",
                (batch_id,),
            ).fetchone()
            return row[0]

    # ------------------------------------------------------------------
    # Item tasks (resumable processing)
    # ------------------------------------------------------------------

    def seed_item_tasks(self, job_id: str, item_keys: list[str]) -> int:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO item_tasks (job_id, item_key) VALUES (?, ?)",
                [(job_id, k) for k in item_keys],
            )
            return len(item_keys)

    def claim_item_task(self, job_id: str) -> str | None:
        """Claim the next pending item. Returns item_key or None."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT item_key FROM item_tasks WHERE job_id = ? AND status = 'pending' LIMIT 1",
                (job_id,),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE item_tasks SET status = 'processing', started_at = ?, attempts = attempts + 1
                       WHERE job_id = ? AND item_key = ?""",
                    (utcnow().isoformat(), job_id, row["item_key"]),
                )
                return row["item_key"]
        return None

    def complete_item_task(self, job_id: str, item_key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE item_tasks SET status = 'done', finished_at = ? WHERE job_id = ? AND item_key = ?",
                (utcnow().isoformat(), job_id, item_key),
            )

    def fail_item_task(self, job_id: str, item_key: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE item_tasks SET status = 'failed', finished_at = ?, error = ? WHERE job_id = ? AND item_key = ?",
                (utcnow().isoformat(), error, job_id, item_key),
            )

    def reset_stale_tasks(self, job_id: str, timeout_seconds: int = 300) -> int:
        """Reset items stuck in 'processing' longer than timeout back to 'pending'."""
        cutoff = (utcnow() - timedelta(seconds=timeout_seconds)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE item_tasks SET status = 'pending'
                   WHERE job_id = ? AND status = 'processing' AND started_at < ?""",
                (job_id, cutoff),
            )
            return cur.rowcount

    def retry_failed_tasks(self, job_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE item_tasks SET status = 'pending', error = NULL WHERE job_id = ? AND status = 'failed'",
                (job_id,),
            )
            return cur.rowcount

    def reset_processing_tasks(self, job_id: str) -> int:
        """Reset in-flight items immediately after a manual stop."""
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE item_tasks
                   SET status = 'pending', started_at = NULL
                   WHERE job_id = ? AND status = 'processing'""",
                (job_id,),
            )
            return cur.rowcount

    def get_item_task_counts(self, job_id: str) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM item_tasks WHERE job_id = ? GROUP BY status",
                (job_id,),
            ).fetchall()
            return {row["status"]: row["cnt"] for row in rows}

    def is_item_done(self, job_id: str, item_key: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT status FROM item_tasks WHERE job_id = ? AND item_key = ?",
                (job_id, item_key),
            ).fetchone()
            return row is not None and row["status"] == "done"

    # ------------------------------------------------------------------
    # Reddit post watching / revisit / hydration
    # ------------------------------------------------------------------

    def upsert_reddit_post(
        self,
        post: dict[str, object],
        *,
        seen_at: str,
        query: str,
        sort: str,
        time_window: str,
        matched: bool,
        policy: dict[str, object],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO reddit_posts (
                    post_id, subreddit, url, title, author, created_timestamp,
                    selftext, domain, flair, first_seen_at, last_seen_at,
                    matched_query, matched, last_sort, last_time_window,
                    last_score, last_num_comments, policy_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    subreddit = excluded.subreddit,
                    url = excluded.url,
                    title = CASE
                        WHEN excluded.title IS NOT NULL AND excluded.title != '' THEN excluded.title
                        ELSE reddit_posts.title
                    END,
                    author = COALESCE(excluded.author, reddit_posts.author),
                    created_timestamp = COALESCE(excluded.created_timestamp, reddit_posts.created_timestamp),
                    selftext = CASE
                        WHEN excluded.selftext IS NOT NULL AND excluded.selftext != '' THEN excluded.selftext
                        ELSE reddit_posts.selftext
                    END,
                    domain = COALESCE(excluded.domain, reddit_posts.domain),
                    flair = COALESCE(excluded.flair, reddit_posts.flair),
                    last_seen_at = excluded.last_seen_at,
                    matched_query = CASE
                        WHEN excluded.matched = 1 THEN excluded.matched_query
                        ELSE reddit_posts.matched_query
                    END,
                    matched = CASE
                        WHEN excluded.matched > reddit_posts.matched THEN excluded.matched
                        ELSE reddit_posts.matched
                    END,
                    last_sort = excluded.last_sort,
                    last_time_window = excluded.last_time_window,
                    last_score = excluded.last_score,
                    last_num_comments = excluded.last_num_comments,
                    policy_json = CASE
                        WHEN excluded.policy_json != '{}' THEN excluded.policy_json
                        ELSE reddit_posts.policy_json
                    END
                """,
                (
                    post.get("id"),
                    post.get("subreddit"),
                    post.get("url"),
                    post.get("title"),
                    post.get("author"),
                    post.get("created_timestamp"),
                    post.get("selftext") or "",
                    post.get("domain"),
                    post.get("link_flair_text"),
                    seen_at,
                    seen_at,
                    query,
                    1 if matched else 0,
                    sort,
                    time_window,
                    int(post.get("score") or 0),
                    int(post.get("num_comments") or 0),
                    json.dumps(policy),
                ),
            )

    def insert_reddit_post_snapshot(
        self,
        post_id: str,
        *,
        seen_at: str,
        sort: str,
        time_window: str,
        score: int,
        num_comments: int,
        snapshot: dict[str, object],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO reddit_post_snapshots (
                    post_id, seen_at, sort, time_window, score, num_comments, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    seen_at,
                    sort,
                    time_window,
                    score,
                    num_comments,
                    json.dumps(snapshot),
                ),
            )

    def get_reddit_post(self, post_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM reddit_posts WHERE post_id = ?",
                (post_id,),
            ).fetchone()
            if row:
                return self._row_to_reddit_post(row)
        return None

    def list_reddit_posts(
        self,
        *,
        subreddit: str | None = None,
        signal_status: str | None = None,
        hydration_status: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        query = "SELECT * FROM reddit_posts"
        clauses: list[str] = []
        params: list[object] = []
        if subreddit:
            clauses.append("subreddit = ?")
            params.append(subreddit)
        if signal_status:
            clauses.append("signal_status = ?")
            params.append(signal_status)
        if hydration_status:
            clauses.append("hydration_status = ?")
            params.append(hydration_status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_reddit_post(row) for row in rows]

    def update_reddit_post_fields(self, post_id: str, **fields: object) -> None:
        if not fields:
            return
        assignments: list[str] = []
        params: list[object] = []
        for key, value in fields.items():
            if key not in {
                "signal_status",
                "hydration_status",
                "next_revisit_at",
                "revisit_count",
                "last_hydrated_at",
                "last_hydration_error",
                "post_payload_json",
                "policy_json",
                "last_score",
                "last_num_comments",
                "last_seen_at",
                "matched_query",
                "matched",
            }:
                continue
            assignments.append(f"{key} = ?")
            if key in {"post_payload_json", "policy_json"} and isinstance(value, dict):
                params.append(json.dumps(value))
            else:
                params.append(value)
        if not assignments:
            return
        params.append(post_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE reddit_posts SET {', '.join(assignments)} WHERE post_id = ?",
                params,
            )

    def schedule_reddit_post_revisit(self, post_id: str, *, next_revisit_at: str | None) -> None:
        self.update_reddit_post_fields(
            post_id,
            signal_status="watching" if next_revisit_at else "seen",
            hydration_status="pending",
            next_revisit_at=next_revisit_at,
        )

    def record_reddit_post_revisit(
        self,
        post_id: str,
        *,
        next_revisit_at: str | None,
        signal_status: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE reddit_posts
                SET revisit_count = revisit_count + 1,
                    next_revisit_at = ?,
                    signal_status = ?,
                    hydration_status = CASE
                        WHEN hydration_status = 'queued' THEN hydration_status
                        ELSE 'pending'
                    END
                WHERE post_id = ?
                """,
                (next_revisit_at, signal_status, post_id),
            )

    def queue_reddit_post_hydration(self, post_id: str) -> None:
        self.update_reddit_post_fields(
            post_id,
            signal_status="queued_for_hydration",
            hydration_status="queued",
            next_revisit_at=None,
        )

    def mark_reddit_post_discarded(self, post_id: str) -> None:
        self.update_reddit_post_fields(
            post_id,
            signal_status="discarded",
            next_revisit_at=None,
        )

    def mark_reddit_post_hydrated(self, post_id: str, payload: dict[str, object]) -> None:
        self.update_reddit_post_fields(
            post_id,
            signal_status="hydrated",
            hydration_status="hydrated",
            next_revisit_at=None,
            last_hydrated_at=utcnow().isoformat(),
            last_hydration_error=None,
            post_payload_json=payload,
        )

    def mark_reddit_post_hydration_failed(self, post_id: str, error: str) -> None:
        self.update_reddit_post_fields(
            post_id,
            hydration_status="failed",
            last_hydration_error=error,
        )

    def get_due_reddit_revisits(
        self,
        *,
        now_iso: str | None = None,
        subreddit: str | None = None,
        limit: int = 20,
    ) -> list[dict]:
        now_iso = now_iso or utcnow().isoformat()
        query = (
            "SELECT * FROM reddit_posts "
            "WHERE signal_status = 'watching' AND next_revisit_at IS NOT NULL AND next_revisit_at <= ?"
        )
        params: list[object] = [now_iso]
        if subreddit:
            query += " AND subreddit = ?"
            params.append(subreddit)
        query += " ORDER BY next_revisit_at ASC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_reddit_post(row) for row in rows]

    def get_queued_reddit_hydrations(
        self,
        *,
        subreddit: str | None = None,
        post_ids: list[str] | None = None,
        limit: int = 20,
    ) -> list[dict]:
        query = "SELECT * FROM reddit_posts WHERE hydration_status = 'queued'"
        params: list[object] = []
        if subreddit:
            query += " AND subreddit = ?"
            params.append(subreddit)
        if post_ids:
            placeholders = ",".join("?" for _ in post_ids)
            query += f" AND post_id IN ({placeholders})"
            params.extend(post_ids)
        query += " ORDER BY last_seen_at DESC LIMIT ?"
        params.append(limit)
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_reddit_post(row) for row in rows]

    @staticmethod
    def _row_to_reddit_post(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["policy_json"] = json.loads(data["policy_json"]) if data["policy_json"] else {}
        data["post_payload_json"] = json.loads(data["post_payload_json"]) if data["post_payload_json"] else {}
        return data

    # ------------------------------------------------------------------

    def linkedin_geo_map_seeded(self) -> bool:
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) FROM linkedin_geo_map").fetchone()
            return row[0] > 0

    def seed_linkedin_geo_map(self, rows: list[tuple[str, str]]) -> int:
        """Bulk insert (lookup_key, autocomplete) pairs."""
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO linkedin_geo_map (lookup_key, autocomplete) VALUES (?, ?)",
                rows,
            )
            return len(rows)

    def get_linkedin_geo(self, lookup_key: str) -> list[str]:
        """Get all autocomplete strings for a lookup key."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT autocomplete FROM linkedin_geo_map WHERE lookup_key = ?",
                (lookup_key.lower(),),
            ).fetchall()
            return [row["autocomplete"] for row in rows]

    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_lead(row: sqlite3.Row) -> Lead:
        return Lead(
            id=row["id"],
            name=row["name"],
            email=row["email"],
            company=row["company"],
            title=row["title"],
            phone=row["phone"],
            location=row["location"],
            linkedin_url=row["linkedin_url"],
            google_maps_url=row["google_maps_url"],
            website=row["website"],
            source=LeadSource(row["source"]),
            raw_data=json.loads(row["raw_data"]) if row["raw_data"] else {},
            scraped_at=datetime.fromisoformat(row["scraped_at"]),
            verification_status=VerificationStatus(row["verification_status"]),
            verified_at=datetime.fromisoformat(row["verified_at"]) if row["verified_at"] else None,
            enriched_at=datetime.fromisoformat(row["enriched_at"]) if row["enriched_at"] else None,
            enrichment_data=json.loads(row["enrichment_data"]) if row["enrichment_data"] else {},
        )

    # ------------------------------------------------------------------
    # Outreach: mailboxes, campaigns, sends, suppression
    # ------------------------------------------------------------------

    def add_mailbox(self, mailbox_id: str, email: str, display_name: str,
                    gws_config_dir: str, daily_limit: int = 10) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sender_mailboxes (id, email, display_name, gws_config_dir, daily_limit, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (mailbox_id, email, display_name, gws_config_dir, daily_limit,
                 utcnow().isoformat()),
            )

    def list_mailboxes(self, active_only: bool = True) -> list[dict]:
        with self._conn() as conn:
            q = "SELECT * FROM sender_mailboxes"
            if active_only:
                q += " WHERE active = 1"
            return [dict(r) for r in conn.execute(q).fetchall()]

    def get_mailbox(self, mailbox_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM sender_mailboxes WHERE id = ?", (mailbox_id,)).fetchone()
            return dict(row) if row else None

    def get_mailbox_sends_today(self, mailbox_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM outreach "
                "WHERE mailbox_id = ? AND status = 'sent' AND DATE(sent_at) = DATE('now')",
                (mailbox_id,),
            ).fetchone()
            return row["cnt"] if row else 0

    def create_campaign(self, campaign_id: str, name: str, filter_json: dict,
                        cadence_json: list, mailbox_id: str | None,
                        physical_address: str, playbook_override: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO campaigns (id, name, filter_json, cadence_json, mailbox_id, "
                "physical_address, playbook_override, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (campaign_id, name, json.dumps(filter_json), json.dumps(cadence_json),
                 mailbox_id, physical_address, playbook_override,
                 utcnow().isoformat()),
            )

    def get_campaign(self, campaign_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if row:
                d = dict(row)
                d["filter_json"] = json.loads(d["filter_json"]) if d["filter_json"] else {}
                d["cadence_json"] = json.loads(d["cadence_json"]) if d["cadence_json"] else []
                return d
        return None

    def list_campaigns(self, status: str | None = None) -> list[dict]:
        with self._conn() as conn:
            if status:
                rows = conn.execute("SELECT * FROM campaigns WHERE status = ? ORDER BY created_at DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d["filter_json"] = json.loads(d["filter_json"]) if d["filter_json"] else {}
                d["cadence_json"] = json.loads(d["cadence_json"]) if d["cadence_json"] else []
                result.append(d)
            return result

    def create_outreach(self, outreach_id: str, campaign_id: str, lead_id: int,
                        mailbox_id: str, step: int, subject: str, body: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO outreach (id, campaign_id, lead_id, mailbox_id, step, "
                "subject, body, status, drafted_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)",
                (outreach_id, campaign_id, lead_id, mailbox_id, step, subject, body,
                 utcnow().isoformat()),
            )

    def approve_outreach(self, outreach_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE outreach SET status = 'approved', approved_at = ? WHERE id = ? AND status = 'draft'",
                (utcnow().isoformat(), outreach_id),
            )
            return cur.rowcount > 0

    def outreach_exists(self, campaign_id: str, lead_id: int, step: int) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM outreach WHERE campaign_id = ? AND lead_id = ? AND step = ?",
                (campaign_id, lead_id, step),
            ).fetchone()
            return row is not None

    def get_previous_sent_message_id(self, campaign_id: str, lead_id: int, step: int) -> str | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT gmail_message_id FROM outreach "
                "WHERE campaign_id = ? AND lead_id = ? AND step < ? "
                "AND status IN ('sent', 'replied') AND gmail_message_id != '' "
                "ORDER BY step DESC LIMIT 1",
                (campaign_id, lead_id, step),
            ).fetchone()
            return row["gmail_message_id"] if row else None

    def get_thread_message_ids(self, gmail_thread_id: str) -> set[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT gmail_message_id FROM outreach WHERE gmail_thread_id = ? AND gmail_message_id != ''",
                (gmail_thread_id,),
            ).fetchall()
            return {row["gmail_message_id"] for row in rows}

    def mark_outreach_sent(self, outreach_id: str, gmail_thread_id: str,
                           gmail_message_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE outreach SET status = 'sent', sent_at = ?, gmail_thread_id = ?, "
                "gmail_message_id = ?, attempt_count = attempt_count + 1 "
                "WHERE id = ? AND status = 'approved'",
                (utcnow().isoformat(), gmail_thread_id, gmail_message_id, outreach_id),
            )

    def mark_outreach_failed(self, outreach_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE outreach SET attempt_count = attempt_count + 1, last_error = ? "
                "WHERE id = ?",
                (error, outreach_id),
            )

    def mark_outreach_replied(self, outreach_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE outreach SET status = 'replied', replied_at = ? WHERE id = ?",
                (utcnow().isoformat(), outreach_id),
            )

    def get_outreach_drafts(self, campaign_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT o.*, l.name as lead_name, l.email as lead_email, l.company as lead_company "
                "FROM outreach o JOIN leads l ON o.lead_id = l.id "
                "WHERE o.campaign_id = ? AND o.status = 'draft' LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_approved_outreach(self, campaign_id: str, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT o.*, l.name as lead_name, l.email as lead_email, l.company as lead_company "
                "FROM outreach o JOIN leads l ON o.lead_id = l.id "
                "WHERE o.campaign_id = ? AND o.status = 'approved' LIMIT ?",
                (campaign_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_outreach_due_followups(self, campaign_id: str) -> list[dict]:
        """Get leads due for follow-up: sent, no reply, past delay window."""
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            return []
        cadence = campaign["cadence_json"]
        max_step = max(s["step"] for s in cadence) if cadence else 1

        with self._conn() as conn:
            # Find latest outreach per lead in this campaign.
            rows = conn.execute(
                "SELECT o.*, l.name as lead_name, l.email as lead_email, l.company as lead_company "
                "FROM outreach o JOIN leads l ON o.lead_id = l.id "
                "WHERE o.campaign_id = ? AND o.status = 'sent' AND o.step < ? "
                "AND NOT EXISTS (SELECT 1 FROM outreach later WHERE later.campaign_id = o.campaign_id "
                "  AND later.lead_id = o.lead_id AND later.step > o.step) "
                "AND NOT EXISTS (SELECT 1 FROM outreach r WHERE r.campaign_id = o.campaign_id "
                "  AND r.lead_id = o.lead_id AND r.status = 'replied') "
                "AND LOWER(l.email) NOT IN (SELECT email FROM suppression_list) "
                "ORDER BY o.sent_at",
                (campaign_id, max_step),
            ).fetchall()

        due = []
        for row in rows:
            d = dict(row)
            current_step = d["step"]
            next_step = current_step + 1
            # Find delay for next step.
            delay = next(
                (s["delay_days"] for s in cadence if s["step"] == next_step), None
            )
            if delay is None:
                continue
            sent_at = datetime.fromisoformat(d["sent_at"])
            due_at = sent_at + timedelta(days=delay)
            if utcnow() >= due_at:
                d["next_step"] = next_step
                d["due_at"] = due_at.isoformat()
                due.append(d)
        return due

    def get_outreach_stats(self, campaign_id: str) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM outreach WHERE campaign_id = ? GROUP BY status",
                (campaign_id,),
            ).fetchall()
            stats = {r["status"]: r["cnt"] for r in rows}
            stats["total"] = sum(stats.values())
            return stats

    def get_sent_threads(self, mailbox_id: str) -> list[dict]:
        """Get all sent outreach with thread IDs for reply checking."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT o.id, o.lead_id, o.gmail_thread_id, o.gmail_message_id, "
                "l.email as lead_email "
                "FROM outreach o JOIN leads l ON o.lead_id = l.id "
                "WHERE o.mailbox_id = ? AND o.status = 'sent' AND o.gmail_thread_id != ''",
                (mailbox_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def is_suppressed(self, email: str) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT 1 FROM suppression_list WHERE email = ?", (email.strip().lower(),),
            ).fetchone()
            return row is not None

    def add_suppression(self, email: str, reason: str = "opt_out", source: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO suppression_list (email, reason, source, created_at) "
                "VALUES (?, ?, ?, ?)",
                (email.strip().lower(), reason, source, utcnow().isoformat()),
            )

    def get_eligible_leads(self, campaign_id: str, limit: int = 20) -> list[dict]:
        """Get leads eligible for outreach: valid email, not suppressed, not already in this campaign."""
        campaign = self.get_campaign(campaign_id)
        if not campaign:
            return []
        filters = campaign["filter_json"]

        with self._conn() as conn:
            # Base: valid email, not suppressed, not already contacted in this campaign.
            q = """
                SELECT l.* FROM leads l
                WHERE l.email IS NOT NULL
                AND l.verification_status IN ('valid', 'catch_all')
                AND LOWER(l.email) NOT IN (SELECT email FROM suppression_list)
                AND l.id NOT IN (SELECT lead_id FROM outreach WHERE campaign_id = ?)
            """
            params: list = [campaign_id]

            # Apply optional filters.
            if filters.get("source"):
                q += " AND l.source = ?"
                params.append(filters["source"])
            if filters.get("enriched_only"):
                q += " AND l.enriched_at IS NOT NULL"

            q += " ORDER BY l.scraped_at DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(q, params).fetchall()
            return [dict(r) for r in rows]
