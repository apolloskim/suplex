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
    linkedin_url TEXT UNIQUE,
    google_maps_url TEXT,
    website TEXT,
    source TEXT NOT NULL,
    raw_data TEXT DEFAULT '{}',
    scraped_at TEXT NOT NULL,
    verification_status TEXT DEFAULT 'pending',
    verified_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_leads_email ON leads(email);
CREATE INDEX IF NOT EXISTS idx_leads_source ON leads(source);
CREATE INDEX IF NOT EXISTS idx_leads_verification ON leads(verification_status);
CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company);
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
        with self._conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO leads (name, email, company, title, phone, location,
                    linkedin_url, google_maps_url, website, source, raw_data,
                    scraped_at, verification_status, verified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(linkedin_url) DO UPDATE SET
                    name=excluded.name, email=excluded.email, company=excluded.company,
                    title=excluded.title, phone=excluded.phone, location=excluded.location,
                    google_maps_url=excluded.google_maps_url, website=excluded.website,
                    raw_data=excluded.raw_data, scraped_at=excluded.scraped_at
                """,
                (
                    lead.name,
                    lead.email,
                    lead.company,
                    lead.title,
                    lead.phone,
                    lead.location,
                    lead.linkedin_url,
                    lead.google_maps_url,
                    lead.website,
                    lead.source.value,
                    json.dumps(lead.raw_data),
                    lead.scraped_at.isoformat(),
                    lead.verification_status.value,
                    lead.verified_at.isoformat() if lead.verified_at else None,
                ),
            )
            return cur.lastrowid  # type: ignore[return-value]

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
