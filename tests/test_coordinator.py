"""Coordinator tests. Scrapers and the Apify budget API are replaced with fakes."""

from __future__ import annotations

import pytest

from suplex.models import Lead, LeadSource, ScrapeTarget
from suplex.scraper import coordinator as coordinator_module
from suplex.scraper.coordinator import ScrapeConfigError, ScrapeCoordinator
from suplex.scraper.gmaps_scraper import DailyLimitReached

BUDGET_OK = {"over_limit": False, "remaining_usd": 4.0, "used_usd": 1.0, "plan_limit_usd": 5.0}
BUDGET_SPENT = {"over_limit": True, "remaining_usd": 0, "used_usd": 5.0, "plan_limit_usd": 5.0}


def _maps(location: str | None = None) -> ScrapeTarget:
    return ScrapeTarget(source=LeadSource.GOOGLE_MAPS, query="dentist", location=location)


def _linkedin(location: str | None = None) -> ScrapeTarget:
    return ScrapeTarget(source=LeadSource.LINKEDIN, query="dentist", location=location)


class FakeScraper:
    """Returns one lead per target and records which targets it was asked for."""

    def __init__(self, source: LeadSource, fail_with: Exception | None = None) -> None:
        self.source = source
        self.fail_with = fail_with
        self.calls: list[ScrapeTarget] = []

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        self.calls.append(target)
        if self.fail_with:
            raise self.fail_with
        return [Lead(name=f"Lead in {target.location}", source=self.source, location=target.location)]


@pytest.fixture
def budget(monkeypatch):
    """Stub the Apify budget endpoint. `budget.calls` counts lookups; `budget.result` sets the answer."""

    class Budget:
        calls = 0
        result: dict = BUDGET_OK

    async def fake_get_apify_budget(token=None):
        Budget.calls += 1
        if isinstance(Budget.result, Exception):
            raise Budget.result
        return Budget.result

    monkeypatch.setattr(coordinator_module, "get_apify_budget", fake_get_apify_budget)
    return Budget


@pytest.fixture
def three_tiles(monkeypatch):
    """Make any located target expand into three fixed tiles (no geo database needed)."""

    def fake_resolve(query, location, *, max_results_per_tile=0, source, store=None):
        return [
            ScrapeTarget(source=source, query=query, location=f"City {n}, TX", extra={"tile_state": "TX"})
            for n in (1, 2, 3)
        ]

    monkeypatch.setattr(coordinator_module, "resolve_tiles", fake_resolve)


async def test_linkedin_without_token_fails_up_front_with_a_clear_message(store) -> None:
    coord = ScrapeCoordinator(store=store)

    with pytest.raises(ScrapeConfigError, match="no Apify token is configured"):
        await coord.run([_linkedin()])


async def test_apify_maps_without_token_is_an_error_not_a_silent_playwright_run(store) -> None:
    coord = ScrapeCoordinator(store=store, scraper="apify")
    coord.gmaps_scraper = FakeScraper(LeadSource.GOOGLE_MAPS)

    with pytest.raises(ScrapeConfigError, match="-s playwright"):
        await coord.run([_maps()])
    assert coord.gmaps_scraper.calls == []


async def test_playwright_choice_is_respected_even_without_a_location(store, budget) -> None:
    # A Maps target with no location is not tiled. It used to fall through to
    # Apify (and spend credit) whenever a token was configured.
    coord = ScrapeCoordinator(store=store, apify_token="tok", scraper="playwright")
    coord.gmaps_scraper = FakeScraper(LeadSource.GOOGLE_MAPS)
    coord.apify_scraper = FakeScraper(LeadSource.GOOGLE_MAPS)

    leads = await coord.run([_maps()])

    assert len(leads) == 1
    assert len(coord.gmaps_scraper.calls) == 1
    assert coord.apify_scraper.calls == []
    assert budget.calls == 0


async def test_budget_is_looked_up_once_per_run_not_once_per_tile(store, budget, three_tiles) -> None:
    coord = ScrapeCoordinator(store=store, apify_token="tok")
    coord.linkedin_scraper = FakeScraper(LeadSource.LINKEDIN)

    leads = await coord.run([_linkedin("Texas")])

    assert len(leads) == 3
    assert budget.calls == 1


async def test_failed_budget_lookup_does_not_block_the_run(store, budget) -> None:
    budget.result = {"error": "Apify API returned 503"}
    coord = ScrapeCoordinator(store=store, apify_token="tok")
    coord.linkedin_scraper = FakeScraper(LeadSource.LINKEDIN)

    leads = await coord.run([_linkedin()])

    assert len(leads) == 1 and coord.failed_targets == {}


async def test_exhausted_budget_marks_tiles_failed_and_job_resumable(store, budget, three_tiles) -> None:
    budget.result = BUDGET_SPENT
    coord = ScrapeCoordinator(store=store, apify_token="tok")
    coord.linkedin_scraper = FakeScraper(LeadSource.LINKEDIN)

    leads = await coord.run([_linkedin("Texas")], job_id="job1")

    assert leads == [] and coord.linkedin_scraper.calls == []
    job = store.get_job("job1")
    assert job["status"] == "done_with_errors"
    assert "3 of 3 targets failed" in job["error"] and "apify_budget_exhausted" in job["error"]
    assert store.get_item_task_counts("job1") == {"failed": 3}


async def test_resume_only_reruns_tiles_that_did_not_finish(store, budget, three_tiles) -> None:
    flaky = FakeScraper(LeadSource.GOOGLE_MAPS)
    real_scrape = flaky.scrape

    async def fail_city_2(target: ScrapeTarget) -> list[Lead]:
        if target.location == "City 2, TX":
            flaky.calls.append(target)
            raise RuntimeError("navigation timeout")
        return await real_scrape(target)

    first = ScrapeCoordinator(store=store)
    first.gmaps_scraper = FakeScraper(LeadSource.GOOGLE_MAPS)
    first.gmaps_scraper.scrape = fail_city_2
    await first.run([_maps("Texas")], job_id="job2")

    assert store.get_job("job2")["status"] == "done_with_errors"
    assert first.failed_targets == {"google_maps:City 2, TX:dentist": "RuntimeError: navigation timeout"}

    second = ScrapeCoordinator(store=store)
    second.gmaps_scraper = FakeScraper(LeadSource.GOOGLE_MAPS)
    await second.run([_maps("Texas")], job_id="job2")

    assert [t.location for t in second.gmaps_scraper.calls] == ["City 2, TX"]
    job = store.get_job("job2")
    assert (job["status"], job["completed"], job["total"]) == ("done", 3, 3)
    assert job["error"] is None  # the first run's error must not linger on a clean rerun
    assert store.count() == 3


async def test_daily_limit_leaves_tiles_unfinished_instead_of_done(store, three_tiles) -> None:
    # Returning [] here used to mark every remaining tile as done with zero leads,
    # so `suplex resume -s apify` had nothing left to pick up.
    coord = ScrapeCoordinator(store=store)
    coord.gmaps_scraper = FakeScraper(LeadSource.GOOGLE_MAPS, fail_with=DailyLimitReached("limit reached"))

    await coord.run([_maps("Texas")], job_id="job3")

    assert store.get_item_task_counts("job3") == {"failed": 3}
    assert store.get_job("job3")["status"] == "done_with_errors"
