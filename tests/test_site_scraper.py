"""Site scraper tests. Network access is replaced with saved HTML fixtures."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from suplex.models import LeadSource, ScrapeTarget
from suplex.scraper import site_scraper
from suplex.scraper.site_scraper import SiteScraper

FIXTURES = Path(__file__).parent / "fixtures"
JS_SHELL = (
    "<html><head><title>Shell Co</title></head><body>"
    '<div id="root"></div><a href="mailto:team@shell.example">mail</a>'
    + "<script>" + "x" * 3000 + "</script></body></html>"
)


@pytest.fixture
def fake_web(monkeypatch):
    """Serve fixture pages instead of making HTTP requests; record what was asked for."""
    pages = {
        "https://acme.example": (FIXTURES / "site_home.html").read_text(),
        "https://acme.example/contact": (FIXTURES / "site_contact.html").read_text(),
        "https://shell.example": JS_SHELL,
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).rstrip("/")
        requested.append(url)
        if url in pages:
            return httpx.Response(200, text=pages[url], headers={"content-type": "text/html"})
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(site_scraper.httpx, "AsyncClient", client_factory)
    return requested


async def test_scrape_accepts_a_bare_domain_and_merges_contact_page(fake_web) -> None:
    leads = await SiteScraper().scrape(ScrapeTarget(source=LeadSource.PLAYWRIGHT, query="acme.example"))

    assert "https://acme.example" in fake_web
    assert "https://acme.example/contact" in fake_web
    assert {lead.email for lead in leads} == {
        "hello@acme.example", "frontdesk@acme.example", "billing@acme.example",
    }
    lead = leads[0]
    assert lead.name == "Acme Dental"
    assert lead.website == "https://acme.example"
    assert lead.location == "1 Main St, Austin, TX, 78701"
    assert lead.raw_data["social_links"] == {"facebook": "https://www.facebook.com/acmedental"}


async def test_malformed_json_ld_blocks_do_not_abort_the_scrape(fake_web) -> None:
    # The fixture has an FAQPage whose mainEntity is an object (not a list) and a
    # Review whose reviewBody is null. Both used to raise and lose the whole site.
    leads = await SiteScraper().scrape(ScrapeTarget(source=LeadSource.PLAYWRIGHT, query="https://acme.example"))

    assert leads[0].raw_data["business_type"] == "Dentist"
    assert leads[0].phone


async def test_failed_browser_fallback_keeps_static_results(fake_web, monkeypatch) -> None:
    async def no_browser(self, url):
        raise RuntimeError("Executable doesn't exist — run `playwright install chromium`")

    monkeypatch.setattr(SiteScraper, "_scrape_playwright", no_browser)

    leads = await SiteScraper().scrape(ScrapeTarget(source=LeadSource.PLAYWRIGHT, query="https://shell.example"))

    assert [lead.email for lead in leads] == ["team@shell.example"]
