"""Apify-backed scrapers. The Apify client is replaced with a fake; nothing is sent."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from suplex.models import LeadSource, ScrapeTarget, VerificationStatus
from suplex.scraper import apify_runner
from suplex.scraper.apify_runner import ApifyError, run_actor
from suplex.scraper.apify_scraper import ApifyScraper
from suplex.scraper.gmaps_scraper import DAILY_LIMIT, DailyLimitReached, GMapsScraper
from suplex.scraper.linkedin_scraper import LinkedInScraper

FIXTURES = Path(__file__).parent / "fixtures"


class FakeApify:
    """Stands in for ApifyClientAsync and records the actor calls made through it."""

    runs: list[tuple[str, dict]] = []
    run_result: dict | None = {"status": "SUCCEEDED", "defaultDatasetId": "ds1"}
    items: list[dict] = []

    def __init__(self, token: str) -> None:
        self.token = token

    def actor(self, actor_id: str):
        outer = self

        class _Actor:
            async def call(self, run_input):
                FakeApify.runs.append((actor_id, run_input))
                return outer.run_result

        return _Actor()

    def dataset(self, dataset_id: str):
        class _Dataset:
            async def list_items(self):
                return type("Page", (), {"items": FakeApify.items})()

        return _Dataset()


@pytest.fixture
def fake_apify(monkeypatch):
    FakeApify.runs = []
    FakeApify.run_result = {"status": "SUCCEEDED", "defaultDatasetId": "ds1"}
    FakeApify.items = []
    monkeypatch.setattr(apify_runner, "ApifyClientAsync", FakeApify)
    return FakeApify


async def test_run_actor_without_token_explains_how_to_fix_it(fake_apify) -> None:
    with pytest.raises(ApifyError, match="suplex setup"):
        await run_actor("", "some/actor", {})
    assert fake_apify.runs == []


async def test_run_actor_reports_a_run_that_never_started(fake_apify) -> None:
    fake_apify.run_result = None

    with pytest.raises(ApifyError, match="did not return a run"):
        await run_actor("tok", "some/actor", {})


async def test_run_actor_keeps_partial_results_of_a_timed_out_run(fake_apify) -> None:
    fake_apify.run_result = {"status": "TIMED-OUT", "defaultDatasetId": "ds1"}
    fake_apify.items = [{"title": "A"}]

    assert await run_actor("tok", "some/actor", {}) == [{"title": "A"}]


async def test_linkedin_scrape_maps_profile_to_lead(fake_apify, store) -> None:
    fake_apify.items = [json.loads((FIXTURES / "linkedin_profile.json").read_text())]
    target = ScrapeTarget(
        source=LeadSource.LINKEDIN, query="dentist AND owner", location="Austin, Texas, United States",
        max_results=20, extra={"li_resolved": True, "seniority_levels": ["320"], "job_titles": ["Owner Dentist"]},
    )

    leads = await LinkedInScraper(token="tok", store=store).scrape(target)

    actor_id, run_input = fake_apify.runs[0]
    assert actor_id == "harvestapi/linkedin-profile-search"
    assert run_input["locations"] == ["Austin, Texas, United States"]
    assert run_input["seniorityLevelIds"] == ["320"]
    assert run_input["maxItems"] == 20
    lead = leads[0]
    assert (lead.name, lead.company, lead.email) == ("Dana Reyes", "Reyes Family Dental", "dana@reyesdental.example")
    assert lead.linkedin_url == "https://www.linkedin.com/in/dana-reyes-dds"
    assert lead.verification_status == VerificationStatus.VALID


async def test_linkedin_scrape_without_token_raises_instead_of_returning_nothing(store) -> None:
    with pytest.raises(ApifyError, match="needs an Apify token"):
        await LinkedInScraper(token="", store=store).scrape(
            ScrapeTarget(source=LeadSource.LINKEDIN, query="dentist"),
        )


async def test_apify_maps_uses_state_polygon_for_us_tiles(fake_apify) -> None:
    fake_apify.items = [{"title": "Acme Dental", "phone": "512-555-0100", "url": "https://maps.example/1"}]
    target = ScrapeTarget(
        source=LeadSource.GOOGLE_MAPS, query="dentist", location="Austin, TX", max_results=10,
        extra={"tile_state": "TX", "tile_country": "US"},
    )

    leads = await ApifyScraper(token="tok").scrape(target)

    _, run_input = fake_apify.runs[0]
    assert run_input["customGeolocation"]["type"] in ("Polygon", "MultiPolygon")
    assert "locationQuery" not in run_input
    assert run_input["maxCrawledPlacesPerSearch"] == 10
    assert leads[0].name == "Acme Dental" and leads[0].google_maps_url == "https://maps.example/1"


async def test_gmaps_scraper_refuses_to_start_past_the_daily_limit(monkeypatch) -> None:
    monkeypatch.setattr(GMapsScraper, "_daily_date", date.today().isoformat())
    monkeypatch.setattr(GMapsScraper, "_daily_count", DAILY_LIMIT)

    with pytest.raises(DailyLimitReached, match="-s apify"):
        await GMapsScraper().scrape(ScrapeTarget(source=LeadSource.GOOGLE_MAPS, query="dentist"))
