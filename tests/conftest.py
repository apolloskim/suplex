"""Shared test setup.

Suplex keeps its database, config and logs under ``~/.config/suplex``. Point
HOME at a throwaway directory *before* any suplex module is imported so a test
can never read or write a developer's real data, even if it forgets to pass an
explicit store.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

_TEST_HOME = tempfile.mkdtemp(prefix="suplex-test-home-")
os.environ["HOME"] = _TEST_HOME
os.environ["USERPROFILE"] = _TEST_HOME
for _var in ("APIFY_TOKEN", "ANTHROPIC_API_KEY", "REACHER_API_KEY", "MILLION_VERIFIER_API_KEY", "SUPLEX_JOB_ID"):
    os.environ.pop(_var, None)

from suplex.models import Lead, LeadSource  # noqa: E402
from suplex.store.db import LeadStore  # noqa: E402


@pytest.fixture
def store(tmp_path: Path) -> LeadStore:
    """An empty lead store backed by a per-test SQLite file."""
    return LeadStore(db_path=tmp_path / "suplex.db")


@pytest.fixture
def make_lead(store: LeadStore):
    """Insert a lead and return it as stored (with its id)."""

    def _make(**fields) -> Lead:
        fields.setdefault("source", LeadSource.GOOGLE_MAPS)
        fields.setdefault("name", "Acme Dental")
        lead_id = store.insert_lead(Lead(**fields))
        return next(lead for lead in store.get_leads() if lead.id == lead_id)

    return _make
