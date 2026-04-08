from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

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
    meta TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

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
                (status.value, datetime.utcnow().isoformat(), lead_id),
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
                (datetime.utcnow().isoformat(), json.dumps(enrichment_data, default=str), lead_id),
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
        now = datetime.utcnow().isoformat()
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
                (query, json.dumps(suggestions), datetime.utcnow().isoformat()),
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
                """,
                (job_id, job_type, total, datetime.utcnow().isoformat(), json.dumps(meta or {})),
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
                (status, datetime.utcnow().isoformat(), error, job_id),
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
                    (datetime.utcnow().isoformat(), job_id, row["item_key"]),
                )
                return row["item_key"]
        return None

    def complete_item_task(self, job_id: str, item_key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE item_tasks SET status = 'done', finished_at = ? WHERE job_id = ? AND item_key = ?",
                (datetime.utcnow().isoformat(), job_id, item_key),
            )

    def fail_item_task(self, job_id: str, item_key: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE item_tasks SET status = 'failed', finished_at = ?, error = ? WHERE job_id = ? AND item_key = ?",
                (datetime.utcnow().isoformat(), error, job_id, item_key),
            )

    def reset_stale_tasks(self, job_id: str, timeout_seconds: int = 300) -> int:
        """Reset items stuck in 'processing' longer than timeout back to 'pending'."""
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(seconds=timeout_seconds)).isoformat()
        with self._conn() as conn:
            cur = conn.execute(
                """UPDATE item_tasks SET status = 'pending'
                   WHERE job_id = ? AND status = 'processing' AND started_at < ?""",
                (job_id, cutoff),
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
        )
