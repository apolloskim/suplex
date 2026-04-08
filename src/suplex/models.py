from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LeadSource(str, Enum):
    GOOGLE_MAPS = "google_maps"
    LINKEDIN = "linkedin"
    PLAYWRIGHT = "playwright"


class VerificationStatus(str, Enum):
    PENDING = "pending"
    VALID = "valid"
    INVALID = "invalid"
    CATCH_ALL = "catch_all"
    UNKNOWN = "unknown"


class Lead(BaseModel):
    id: int | None = None
    name: str | None = None
    email: str | None = None
    company: str | None = None
    title: str | None = None
    phone: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    google_maps_url: str | None = None
    website: str | None = None
    source: LeadSource
    raw_data: dict[str, Any] = Field(default_factory=dict)
    scraped_at: datetime = Field(default_factory=datetime.utcnow)
    verification_status: VerificationStatus = VerificationStatus.PENDING
    verified_at: datetime | None = None


class ScrapeTarget(BaseModel):
    """A single scrape job — what to scrape and how."""

    source: LeadSource
    query: str
    location: str | None = None
    max_results: int = 100
    extra: dict[str, Any] = Field(default_factory=dict)


class ScrapePlan(BaseModel):
    """AI-generated plan for what to scrape."""

    reasoning: str
    targets: list[ScrapeTarget]
    schedule_cron: str | None = None


class VerificationResult(BaseModel):
    email: str
    status: VerificationStatus
    reason: str | None = None
    mx_host: str | None = None
    is_disposable: bool = False
    is_catch_all: bool = False
