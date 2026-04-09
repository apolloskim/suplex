"""CLI entry point — agents and humans call this."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import click

from suplex.models import LeadSource, ScrapeTarget, VerificationStatus
from suplex.store.db import LeadStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Suplex — Agent-driven lead scraping and verification."""
    from pathlib import Path
    config_path = Path.home() / ".config" / "suplex" / "config.toml"
    skill_path = Path.home() / ".claude" / "skills" / "suplex" / "SKILL.md"
    is_configured = config_path.exists() and skill_path.exists()

    # If called with no subcommand (just `suplex`), show getting started.
    if ctx.invoked_subcommand is None:
        if not is_configured:
            click.echo("Welcome to Suplex! Get started:\n", err=True)
            click.echo("  suplex setup    Install skill, MCP server, and configure API keys", err=True)
            click.echo("  suplex doctor   Check service health\n", err=True)
        else:
            # Show help if already configured.
            click.echo(ctx.get_help())
        return

    # First-run nudge on any other command.
    if not is_configured and ctx.invoked_subcommand not in ("setup", "serve", "doctor"):
        click.echo("Suplex not fully configured. Run: suplex setup", err=True)


# ---------------------------------------------------------------------------
# profile (sender identity)
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("url")
def profile(url: str) -> None:
    """Scrape your website to create a sender profile."""
    from suplex.profile import scrape_sender_profile

    async def _run() -> None:
        click.echo(f"Scraping {url} for sender profile...", err=True)
        p = await scrape_sender_profile(url)
        click.echo(f"Sender profile saved:", err=True)
        click.echo(f"  Business: {p.business_name}", err=True)
        click.echo(f"  Description: {p.description[:120]}", err=True)
        click.echo(f"  Emails: {p.data.get('emails', [])}", err=True)
        click.echo(json.dumps(p.summary(), indent=2))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# setup & doctor
# ---------------------------------------------------------------------------


@cli.command()
def setup() -> None:
    """Full setup — install skill, register MCP server, configure API keys."""
    import shutil
    import subprocess
    from pathlib import Path

    from suplex.config import Config
    from suplex.setup import SERVICES, validate_key

    # Step 1: Install skill files.
    click.echo("Installing suplex skill...", err=True)
    skill_src = Path(__file__).parent / "skills" / "suplex"
    if skill_src.exists():
        # Install to ~/.claude/skills/ (Claude Code) and ~/.agents/skills/ (universal)
        for base in (Path.home() / ".claude" / "skills", Path.home() / ".agents" / "skills"):
            dst = base / "suplex"
            dst.mkdir(parents=True, exist_ok=True)
            for f in skill_src.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
        click.echo("  Skill installed.", err=True)
    else:
        click.echo("  Skill files not found in package — skipping.", err=True)

    # Step 2: Register MCP server (if claude CLI available).
    click.echo("Registering MCP server...", err=True)
    suplex_bin = shutil.which("suplex")
    claude_bin = shutil.which("claude")
    if claude_bin and suplex_bin:
        try:
            subprocess.run(
                [claude_bin, "mcp", "add", "suplex", "--scope", "user", "--", suplex_bin, "serve"],
                capture_output=True, timeout=10,
            )
            click.echo("  MCP server registered with Claude Code.", err=True)
        except Exception:
            click.echo("  Could not register MCP server automatically.", err=True)
            click.echo(f"  Run manually: claude mcp add suplex --scope user -- {suplex_bin} serve", err=True)
    else:
        if suplex_bin:
            click.echo(f"  Claude CLI not found. Register manually:", err=True)
            click.echo(f"  claude mcp add suplex --scope user -- {suplex_bin} serve", err=True)
        else:
            click.echo("  suplex not found on PATH — install globally first: uv tool install suplex-ai", err=True)

    # Step 3: Install Playwright browsers.
    click.echo("Checking Playwright browsers...", err=True)
    try:
        result = subprocess.run(
            ["playwright", "install", "chromium"], capture_output=True, timeout=120,
        )
        if result.returncode == 0:
            click.echo("  Chromium ready.", err=True)
        else:
            click.echo("  Run: playwright install chromium", err=True)
    except Exception:
        click.echo("  Run: playwright install chromium", err=True)

    # Step 4: Configure API keys.
    click.echo("\nConfiguring API keys...", err=True)
    config = Config.load()

    for key, meta in SERVICES.items():
        current = getattr(config, key)
        required = "(required)" if meta["required"] else "(optional)"
        name = meta["name"]

        if current:
            click.echo(f"  {name}: already configured {required}", err=True)
            if not click.confirm(f"  Reconfigure {name}?", default=False):
                continue

        click.echo(f"\n  {name} {required}", err=True)
        click.echo(f"  {meta['description']}", err=True)
        click.echo(f"  Sign up: {meta['signup_url']}", err=True)
        click.echo(f"  Get key: {meta['key_url']}", err=True)

        if not meta["required"]:
            if not click.confirm(f"  Configure {name}?", default=False):
                continue

        value = click.prompt(f"  {name} key", hide_input=True)
        if not value.strip():
            continue

        click.echo(f"  Validating...", err=True)
        result = asyncio.run(validate_key(key, value.strip()))
        if result["valid"]:
            setattr(config, key, value.strip())
            click.echo(f"  {name} validated!", err=True)
        else:
            click.echo(f"  {name} validation failed: {result.get('error')}", err=True)
            if click.confirm("  Save anyway?", default=False):
                setattr(config, key, value.strip())

    config.save()
    click.echo("\nConfig saved to ~/.config/suplex/config.toml", err=True)

    # Offer to set up Reacher Docker if no Reacher key.
    if not config.reacher_api_key:
        click.echo("\n  No Reacher API key — checking Docker for free self-hosted verification...", err=True)
        _check_reacher_docker()


@cli.command()
def doctor() -> None:
    """Check health of all services (API keys, Docker, Reacher)."""
    import shutil

    from suplex.config import Config
    from suplex.setup import SERVICES

    config = Config.load()

    click.echo("Services:", err=True)
    for key, meta in SERVICES.items():
        configured = bool(getattr(config, key))
        status = "ok" if configured else ("MISSING" if meta["required"] else "skipped")
        click.echo(f"  {meta['name']}: {status}", err=True)

    click.echo("\nDocker:", err=True)
    if shutil.which("docker"):
        click.echo("  docker: found", err=True)
        _check_reacher_docker()
    else:
        click.echo("  docker: not found", err=True)
        click.echo("  Install: https://docs.docker.com/get-docker/", err=True)

    click.echo("\nDatabase:", err=True)
    store = LeadStore()
    click.echo(f"  path: {store.db_path}", err=True)
    click.echo(f"  leads: {store.count()}", err=True)


def _check_reacher_docker() -> None:
    """Check if Reacher Docker container is running, offer to start it."""
    import shutil
    import subprocess

    if not shutil.which("docker"):
        click.echo("  Docker not installed.", err=True)
        click.echo("  Install Docker: https://docs.docker.com/get-docker/", err=True)
        click.echo("  Then run: suplex doctor", err=True)
        return

    # Check if container exists.
    result = subprocess.run(
        ["docker", "inspect", "suplex-reacher"],
        capture_output=True,
    )

    if result.returncode == 0:
        # Check if running.
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "suplex-reacher"],
            capture_output=True,
            text=True,
        )
        if "true" in result.stdout:
            click.echo("  Reacher Docker: running at localhost:8080", err=True)
        else:
            click.echo("  Reacher Docker: stopped", err=True)
            if click.confirm("  Start it?", default=True):
                subprocess.run(["docker", "start", "suplex-reacher"], capture_output=True)
                click.echo("  Started!", err=True)
    else:
        click.echo("  Reacher Docker: not installed", err=True)
        if click.confirm("  Pull and start Reacher? (free, ~50MB)", default=True):
            click.echo("  Pulling reacherhq/backend...", err=True)
            subprocess.run(
                ["docker", "run", "-d",
                 "--name", "suplex-reacher",
                 "-p", "8080:8080",
                 "--restart", "unless-stopped",
                 "reacherhq/backend:latest"],
            )
            click.echo("  Reacher running at localhost:8080", err=True)


# ---------------------------------------------------------------------------
# scrape
# ---------------------------------------------------------------------------


@cli.group()
def scrape() -> None:
    """Scrape leads from various sources."""


@scrape.command("maps")
@click.argument("query")
@click.option("--location", "-l", default=None, help="Location filter")
@click.option("--max-results", "-n", default=0, help="Max results per tile (0=exhaust)")
@click.option("--scraper", "-s", type=click.Choice(["playwright", "apify"]), default="playwright", help="Force scraper: playwright (free) or apify (paid)")
@click.option("--reviews", "-r", default=5, help="Reviews per place (0=skip)")
@click.option("--only", default=None, help="Only include these regions (comma-separated: states, regions, or country codes)")
@click.option("--exclude", default=None, help="Exclude these regions (comma-separated: states, regions, or country codes)")
@click.option("--enrich/--no-enrich", default=True, help="Auto-enrich after scraping (default: yes)")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def scrape_maps(query: str, location: str | None, max_results: int, scraper: str, reviews: int, only: str | None, exclude: str | None, enrich: bool, apify_token: str | None) -> None:
    """Scrape Google Maps for business leads."""
    from suplex.config import Config
    from suplex.scraper.coordinator import ScrapeCoordinator

    token = apify_token or Config.load().apify_token
    store = LeadStore()
    coord = ScrapeCoordinator(store=store, apify_token=token, scraper=scraper, reviews_per_place=reviews)
    extra: dict = {}
    if only:
        extra["only_states"] = [s.strip().upper() for s in only.split(",")]
    if exclude:
        extra["exclude_states"] = [s.strip().upper() for s in exclude.split(",")]
    target = ScrapeTarget(
        source=LeadSource.GOOGLE_MAPS,
        query=query,
        location=location,
        max_results=max_results,
        extra=extra,
    )
    job_id = os.environ.get("SUPLEX_JOB_ID")
    leads = asyncio.run(coord.run([target], job_id=job_id))
    click.echo(json.dumps({"leads": len(leads)}, indent=2))

    # Auto-enrich unless --no-enrich or SUPLEX_NO_ENRICH env.
    if enrich and not os.environ.get("SUPLEX_NO_ENRICH"):
        _auto_enrich(store, token)


@scrape.command("linkedin")
@click.argument("query")
@click.option("--location", "-l", default=None, help="Location (city, state, or country — auto-tiled)")
@click.option("--titles", "-t", default=None, help="Current job titles filter (comma-separated)")
@click.option("--past-titles", default=None, help="Past job titles filter (comma-separated)")
@click.option("--companies", "-c", default=None, help="Current companies (comma-separated LinkedIn URLs)")
@click.option("--past-companies", default=None, help="Past companies (comma-separated LinkedIn URLs)")
@click.option("--industries", default=None, help="Industry IDs (comma-separated, e.g. 4=Software,43=Financial)")
@click.option("--seniority", default=None, help="Seniority level IDs (comma-separated, e.g. 320=Owner/Partner,310=CXO)")
@click.option("--functions", default=None, help="Function IDs (comma-separated, e.g. 11=Healthcare,8=Engineering)")
@click.option("--headcount", default=None, help="Company headcount codes (comma-separated, e.g. A=Self-employed,B=1-10)")
@click.option("--schools", default=None, help="Schools filter (comma-separated)")
@click.option("--years-exp", default=None, help="Years of experience IDs (comma-separated, 1=<1yr,2=1-2,3=3-5,4=6-10,5=10+)")
@click.option("--years-at-company", default=None, help="Years at current company IDs (comma-separated, same scale)")
@click.option("--company-hq", default=None, help="Company HQ locations (comma-separated)")
@click.option("--profile-languages", default=None, help="Profile languages (comma-separated, e.g. English,Spanish)")
@click.option("--recently-changed-jobs", is_flag=True, default=False, help="Only people who recently changed jobs")
@click.option("--mode", "-m", type=click.Choice(["short", "full", "full_with_email"]), default="full_with_email")
@click.option("--max-results", "-n", default=0, help="Max results (0 = exhaust)")
@click.option("--only", default=None, help="Only include these states (comma-separated)")
@click.option("--exclude", default=None, help="Exclude these states (comma-separated)")
@click.option("--enrich/--no-enrich", default=True, help="Auto-enrich after scraping (default: yes)")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def scrape_linkedin(
    query: str, location: str | None, titles: str | None, past_titles: str | None,
    companies: str | None, past_companies: str | None, industries: str | None,
    seniority: str | None, functions: str | None, headcount: str | None,
    schools: str | None, years_exp: str | None, years_at_company: str | None,
    company_hq: str | None, profile_languages: str | None, recently_changed_jobs: bool,
    mode: str, max_results: int, only: str | None, exclude: str | None,
    enrich: bool, apify_token: str | None,
) -> None:
    """Scrape LinkedIn for people leads.

    QUERY supports LinkedIn boolean operators: AND, OR, NOT, "quotes", (parens).
    Example: "dentist AND owner", "CEO NOT (assistant OR SVP)"
    """
    from suplex.config import Config
    from suplex.scraper.coordinator import ScrapeCoordinator

    token = apify_token or Config.load().apify_token
    store = LeadStore()
    coord = ScrapeCoordinator(store=store, apify_token=token)

    extra: dict = {"mode": mode}
    if titles:
        extra["job_titles"] = [t.strip() for t in titles.split(",")]
    if past_titles:
        extra["past_job_titles"] = [t.strip() for t in past_titles.split(",")]
    if companies:
        extra["current_companies"] = [c.strip() for c in companies.split(",")]
    if past_companies:
        extra["past_companies"] = [c.strip() for c in past_companies.split(",")]
    if industries:
        extra["industries"] = [i.strip() for i in industries.split(",")]
    if seniority:
        extra["seniority_levels"] = [s.strip() for s in seniority.split(",")]
    if functions:
        extra["functions"] = [f.strip() for f in functions.split(",")]
    if headcount:
        extra["company_headcount"] = [h.strip() for h in headcount.split(",")]
    if schools:
        extra["schools"] = [s.strip() for s in schools.split(",")]
    if years_exp:
        extra["years_experience"] = [y.strip() for y in years_exp.split(",")]
    if years_at_company:
        extra["years_at_company"] = [y.strip() for y in years_at_company.split(",")]
    if company_hq:
        extra["company_hq_locations"] = [l.strip() for l in company_hq.split(",")]
    if profile_languages:
        extra["profile_languages"] = [l.strip() for l in profile_languages.split(",")]
    if recently_changed_jobs:
        extra["recently_changed_jobs"] = True
    if only:
        extra["only_states"] = [s.strip().upper() for s in only.split(",")]
    if exclude:
        extra["exclude_states"] = [s.strip().upper() for s in exclude.split(",")]

    target = ScrapeTarget(
        source=LeadSource.LINKEDIN,
        query=query,
        location=location,
        max_results=max_results,
        extra=extra,
    )
    job_id = os.environ.get("SUPLEX_JOB_ID")
    leads = asyncio.run(coord.run([target], job_id=job_id))
    click.echo(json.dumps({"leads": len(leads)}, indent=2))

    if enrich and not os.environ.get("SUPLEX_NO_ENRICH"):
        _auto_enrich(store, token)


@scrape.command("website")
@click.argument("url")
def scrape_website(url: str) -> None:
    """Scrape a website for contact info."""
    from suplex.scraper.coordinator import ScrapeCoordinator

    store = LeadStore()
    coord = ScrapeCoordinator(store=store)
    target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=url)
    leads = asyncio.run(coord.run([target]))
    click.echo(json.dumps([l.model_dump() for l in leads], default=str, indent=2))


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("domain")
@click.option("--preferences", "-p", default="", help="Target preferences")
@click.option("--execute", "-x", is_flag=True, help="Execute the plan immediately after generating")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def plan(domain: str, preferences: str, execute: bool, apify_token: str | None) -> None:
    """AI-generate a scrape plan for your business domain."""
    from suplex.ai.strategist import Strategist
    from suplex.scraper.coordinator import ScrapeCoordinator

    async def _run() -> None:
        s = Strategist()
        p = await s.plan(domain=domain, preferences=preferences)
        click.echo(json.dumps(p.model_dump(), default=str, indent=2))

        if execute:
            click.echo("\n--- Executing plan ---\n", err=True)
            store = LeadStore()
            coord = ScrapeCoordinator(store=store, apify_token=apify_token)
            leads = await coord.run(p.targets)
            click.echo(json.dumps({"leads_scraped": len(leads)}, indent=2))

    asyncio.run(_run())


def _auto_enrich(store: LeadStore, apify_token: str | None) -> None:
    """Run enrichment on unenriched leads after scraping."""
    from suplex.enrich.pipeline import EnrichmentPipeline

    unenriched = store.get_unenriched(limit=1)
    if not unenriched:
        return

    click.echo("Auto-enriching scraped leads...", err=True)
    pipeline = EnrichmentPipeline(store=store, apify_token=apify_token, linkedin_posts=1)
    job_id = os.environ.get("SUPLEX_JOB_ID")
    enrich_job_id = f"{job_id}_enrich" if job_id else None
    stats = asyncio.run(pipeline.run(limit=9999, job_id=enrich_job_id))
    click.echo(json.dumps(stats, default=str, indent=2))


# ---------------------------------------------------------------------------
# enrich
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", "-n", default=100, help="Max leads to enrich")
@click.option("--linkedin-posts", "-lp", default=1, help="LinkedIn posts per lead (0=skip)")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def enrich(limit: int, linkedin_posts: int, apify_token: str | None) -> None:
    """Enrich unenriched leads: site scrape + email verify + email guess + LinkedIn posts."""
    from suplex.config import Config
    from suplex.enrich.pipeline import EnrichmentPipeline

    token = apify_token or Config.load().apify_token

    async def _run() -> None:
        pipeline = EnrichmentPipeline(
            apify_token=token,
            linkedin_posts=linkedin_posts,
        )
        job_id = os.environ.get("SUPLEX_JOB_ID")
        stats = await pipeline.run(limit=limit, job_id=job_id)
        click.echo(json.dumps(stats, indent=2))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--limit", "-n", default=50, help="Max emails to verify")
def verify(limit: int) -> None:
    """Verify unverified emails in the database."""
    from suplex.verify.verifier import EmailVerifier

    async def _run() -> None:
        store = LeadStore()
        v = EmailVerifier()
        unverified = store.get_unverified_emails(limit=limit)
        click.echo(f"Verifying {len(unverified)} emails...", err=True)

        for lead in unverified:
            if lead.email and lead.id:
                result = await v.verify(lead.email)
                store.update_verification(lead.id, result.status)
                symbol = {"valid": "+", "invalid": "x", "catch_all": "?", "unknown": "~"}
                click.echo(f"  [{symbol.get(result.status.value, '?')}] {lead.email} — {result.reason}", err=True)

        click.echo(json.dumps({"verified": len(unverified)}, indent=2))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# leads
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--source", "-s", type=click.Choice(["google_maps", "linkedin", "playwright"]), default=None)
@click.option("--status", "-v", type=click.Choice(["pending", "valid", "invalid", "catch_all", "unknown"]), default=None)
@click.option("--limit", "-n", default=100)
def leads(source: str | None, status: str | None, limit: int) -> None:
    """List leads from the database."""
    store = LeadStore()
    src = LeadSource(source) if source else None
    vs = VerificationStatus(status) if status else None
    results = store.get_leads(source=src, verification_status=vs, limit=limit)
    click.echo(json.dumps([l.model_dump() for l in results], default=str, indent=2))


@cli.command()
def stats() -> None:
    """Show lead count statistics."""
    store = LeadStore()
    data: dict[str, int] = {}
    for src in LeadSource:
        for vs in VerificationStatus:
            count = store.count(source=src, verification_status=vs)
            if count > 0:
                data[f"{src.value}/{vs.value}"] = count
    data["total"] = store.count()
    click.echo(json.dumps(data, indent=2))


@cli.command()
@click.option("--source", "-s", type=click.Choice(["google_maps", "linkedin", "playwright"]), default=None, help="Filter by source")
@click.option("--status", "-v", type=click.Choice(["pending", "valid", "invalid", "catch_all", "unknown"]), default=None, help="Filter by verification status")
@click.option("--enriched", "-e", is_flag=True, default=False, help="Only enriched leads")
@click.option("--output", "-o", default="leads.csv", help="Output file path")
def export(source: str | None, status: str | None, enriched: bool, output: str) -> None:
    """Export leads to CSV."""
    import csv
    from suplex.models import LeadSource, VerificationStatus

    store = LeadStore()
    src = LeadSource(source) if source else None
    vs = VerificationStatus(status) if status else None
    leads_list = store.get_leads(source=src, verification_status=vs, limit=999999)

    if enriched:
        leads_list = [l for l in leads_list if l.raw_data.get("enrichment_data") or l.raw_data.get("clean_text")]

    if not leads_list:
        click.echo("No leads to export.", err=True)
        return

    # Build CSV with flat columns for the most useful fields.
    fieldnames = [
        "name", "email", "verification_status", "company", "title", "phone",
        "location", "website", "source", "linkedin_url", "google_maps_url",
        "rating", "review_count", "scraped_at",
    ]

    with open(output, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for lead in leads_list:
            row = {
                "name": lead.name,
                "email": lead.email,
                "verification_status": lead.verification_status.value,
                "company": lead.company,
                "title": lead.title,
                "phone": lead.phone,
                "location": lead.location,
                "website": lead.website,
                "source": lead.source.value,
                "linkedin_url": lead.linkedin_url,
                "google_maps_url": lead.google_maps_url,
                "rating": lead.raw_data.get("rating", ""),
                "review_count": lead.raw_data.get("review_count", ""),
                "scraped_at": lead.scraped_at.isoformat() if lead.scraped_at else "",
            }
            writer.writerow(row)

    click.echo(f"Exported {len(leads_list)} leads to {output}", err=True)


@cli.command()
@click.argument("query", default=None, required=False)
@click.option("--search", "-q", default=None, help="Search for a city, region, or country")
def regions(query: str | None, search: str | None) -> None:
    """List regions in a country, or search for any location."""
    store = LeadStore()

    if search:
        # Search cities table for matching city/region/country.
        import unicodedata
        term = "".join(
            c for c in unicodedata.normalize("NFD", search.lower()) if unicodedata.category(c) != "Mn"
        )
        with store._conn() as conn:
            rows = conn.execute(
                """SELECT name, state, country_code, country_name, population
                   FROM cities WHERE LOWER(name) LIKE ? OR LOWER(state) LIKE ? OR LOWER(country_name) LIKE ?
                   ORDER BY population DESC LIMIT 20""",
                (f"%{term}%", f"%{term}%", f"%{term}%"),
            ).fetchall()

        if not rows:
            click.echo(f"No results for '{search}'", err=True)
            return

        click.echo(f"Results for '{search}':", err=True)
        for r in rows:
            click.echo(f"  {r['name']:25s} region={r['state']:25s} country={r['country_name']} ({r['country_code']})", err=True)

        # Also check LinkedIn geo map.
        li_results = store.get_linkedin_geo(search.lower())
        if li_results:
            click.echo(f"\nLinkedIn autocompletes for '{search}':", err=True)
            for li in li_results[:10]:
                click.echo(f"  {li}", err=True)

    elif query:
        # List regions in a country.
        from suplex.geo.tiles import _get_country_codes
        codes = _get_country_codes(store)
        cc = codes.get(query.lower())
        if not cc:
            # Maybe it's a direct country code.
            cc = query.upper() if len(query) <= 3 else None

        if not cc:
            click.echo(f"Unknown country: {query}", err=True)
            return

        region_list = store.get_regions(cc)
        country_name = next((c["country_name"] for c in store.get_all_countries() if c["country_code"] == cc), cc)
        click.echo(f"{country_name} ({cc}): {len(region_list)} regions", err=True)
        for r in region_list:
            cities = store.get_cities(r, country_code=cc)
            click.echo(f"  {r:30s} ({len(cities)} cities)", err=True)
    else:
        click.echo("Usage: suplex regions <country> or suplex regions --search <query>", err=True)


@cli.command()
def budget() -> None:
    """Check Apify credit usage and remaining budget."""
    from suplex.apify_budget import get_apify_budget

    result = asyncio.run(get_apify_budget())
    if result.get("error"):
        click.echo(f"Error: {result['error']}", err=True)
        return

    click.echo(f"Apify Budget:", err=True)
    click.echo(f"  Used:      ${result['used_usd']:.2f} / ${result['plan_limit_usd']:.2f}", err=True)
    click.echo(f"  Remaining: ${result['remaining_usd']:.2f}", err=True)
    if result["over_limit"]:
        click.echo(f"  WARNING: Over monthly limit!", err=True)
    click.echo(f"  Active runs: {result['active_runs']}", err=True)
    click.echo(f"  Cycle: {result['cycle_start'][:10]} to {result['cycle_end'][:10]}", err=True)
    click.echo(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# run (background jobs)
# ---------------------------------------------------------------------------


@cli.group()
def run() -> None:
    """Launch background jobs (scrape/enrich) that survive terminal closure."""


@run.command("scrape-maps")
@click.argument("query")
@click.option("--location", "-l", required=True, help="Location")
@click.option("--max-results", "-n", default=0, help="Max results per tile (0=exhaust)")
@click.option("--scraper", "-s", type=click.Choice(["playwright", "apify"]), default="playwright")
@click.option("--no-enrich", is_flag=True, default=False, help="Skip auto-enrichment")
def run_scrape_maps(query: str, location: str, max_results: int, scraper: str, no_enrich: bool) -> None:
    """Launch Google Maps scrape + enrich in background."""
    from suplex.jobs import launch_scrape_job
    extra = []
    if max_results > 0:
        extra.extend(["-n", str(max_results)])
    extra.extend(["-s", scraper])
    if no_enrich:
        extra.append("--no-enrich")
    result = launch_scrape_job("maps", query, location, max_results=max_results, extra_args=extra)
    click.echo(json.dumps(result, indent=2))


@run.command("scrape-linkedin")
@click.argument("query")
@click.option("--location", "-l", required=True, help="Location")
@click.option("--titles", "-t", default=None, help="Current job titles (comma-separated)")
@click.option("--past-titles", default=None, help="Past job titles (comma-separated)")
@click.option("--companies", "-c", default=None, help="Current companies (comma-separated)")
@click.option("--past-companies", default=None, help="Past companies (comma-separated)")
@click.option("--industries", default=None, help="Industry IDs (comma-separated)")
@click.option("--seniority", default=None, help="Seniority level IDs (comma-separated)")
@click.option("--functions", default=None, help="Function IDs (comma-separated)")
@click.option("--headcount", default=None, help="Company headcount codes (comma-separated)")
@click.option("--schools", default=None, help="Schools filter (comma-separated)")
@click.option("--years-exp", default=None, help="Years of experience IDs (comma-separated)")
@click.option("--years-at-company", default=None, help="Years at current company IDs (comma-separated)")
@click.option("--company-hq", default=None, help="Company HQ locations (comma-separated)")
@click.option("--profile-languages", default=None, help="Profile languages (comma-separated)")
@click.option("--recently-changed-jobs", is_flag=True, default=False, help="Recently changed jobs filter")
@click.option("--max-results", "-n", default=0, help="Max results (0=exhaust)")
@click.option("--only", default=None, help="Only include these regions (comma-separated)")
@click.option("--exclude", default=None, help="Exclude these regions (comma-separated)")
@click.option("--no-enrich", is_flag=True, default=False, help="Skip auto-enrichment")
def run_scrape_linkedin(
    query: str, location: str, titles: str | None, past_titles: str | None,
    companies: str | None, past_companies: str | None, industries: str | None,
    seniority: str | None, functions: str | None, headcount: str | None,
    schools: str | None, years_exp: str | None, years_at_company: str | None,
    company_hq: str | None, profile_languages: str | None, recently_changed_jobs: bool,
    max_results: int, only: str | None, exclude: str | None, no_enrich: bool,
) -> None:
    """Launch LinkedIn scrape + enrich in background.

    QUERY supports LinkedIn boolean operators: AND, OR, NOT, "quotes", (parens).
    """
    from suplex.jobs import launch_scrape_job
    extra = []
    if titles:
        extra.extend(["-t", titles])
    if past_titles:
        extra.extend(["--past-titles", past_titles])
    if companies:
        extra.extend(["-c", companies])
    if past_companies:
        extra.extend(["--past-companies", past_companies])
    if industries:
        extra.extend(["--industries", industries])
    if seniority:
        extra.extend(["--seniority", seniority])
    if functions:
        extra.extend(["--functions", functions])
    if headcount:
        extra.extend(["--headcount", headcount])
    if schools:
        extra.extend(["--schools", schools])
    if years_exp:
        extra.extend(["--years-exp", years_exp])
    if years_at_company:
        extra.extend(["--years-at-company", years_at_company])
    if company_hq:
        extra.extend(["--company-hq", company_hq])
    if profile_languages:
        extra.extend(["--profile-languages", profile_languages])
    if recently_changed_jobs:
        extra.append("--recently-changed-jobs")
    if max_results > 0:
        extra.extend(["-n", str(max_results)])
    if only:
        extra.extend(["--only", only])
    if exclude:
        extra.extend(["--exclude", exclude])
    if no_enrich:
        extra.append("--no-enrich")
    result = launch_scrape_job("linkedin", query, location, max_results=max_results, extra_args=extra)
    click.echo(json.dumps(result, indent=2))


@run.command("enrich")
@click.option("--limit", "-n", default=100, help="Max leads")
@click.option("--linkedin-posts", "-lp", default=1, help="Posts per lead")
def run_enrich(limit: int, linkedin_posts: int) -> None:
    """Launch enrichment in background."""
    from suplex.jobs import launch_enrich_job
    result = launch_enrich_job(limit=limit, linkedin_posts=linkedin_posts)
    click.echo(json.dumps(result, indent=2))


@cli.command("jobs")
@click.option("--status", "-s", type=click.Choice(["running", "done", "failed", "pending"]), default=None)
def list_jobs_cmd(status: str | None) -> None:
    """List recent jobs."""
    from suplex.jobs import list_jobs
    jobs = list_jobs(status=status)
    if not jobs:
        click.echo("No jobs found.", err=True)
        return
    for j in jobs:
        pct = f"{j['completed']}/{j['total']}" if j["total"] > 0 else "—"
        click.echo(f"  {j['id']}  {j['status']:8s}  {j['type']:8s}  {pct:>12s}  leads={j['leads_count']}  {j.get('started_at', '')[:19]}")


@cli.command("job")
@click.argument("job_id")
def job_status_cmd(job_id: str) -> None:
    """Check status of a specific job."""
    from suplex.jobs import get_job_status
    result = get_job_status(job_id)
    click.echo(json.dumps(result, indent=2))


@cli.command("resume")
@click.argument("job_id")
@click.option("--scraper", "-s", type=click.Choice(["playwright", "apify"]), default=None, help="Override scraper (e.g., switch from playwright to apify)")
def resume_cmd(job_id: str, scraper: str | None) -> None:
    """Resume a stopped or failed job. Picks up where it left off.

    Use -s to switch scraper: e.g., hit 500 playwright limit, resume with apify.
    """
    from suplex.jobs import resume_job
    result = resume_job(job_id, scraper_override=scraper)
    click.echo(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# serve (MCP server)
# ---------------------------------------------------------------------------


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport) for AI agents."""
    from suplex.mcp_server import main as mcp_main

    mcp_main()


