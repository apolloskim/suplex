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


@click.group()
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Suplex — Agent-driven lead scraping and verification."""
    # First-run detection: nudge user to setup if not configured.
    if ctx.invoked_subcommand not in ("setup", "serve", "doctor"):
        from pathlib import Path
        config_path = Path.home() / ".config" / "suplex" / "config.toml"
        skill_path = Path.home() / ".claude" / "skills" / "suplex" / "SKILL.md"
        if not config_path.exists() or not skill_path.exists():
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
@click.option("--scraper", "-s", type=click.Choice(["auto", "playwright", "apify"]), default="auto", help="Force scraper: playwright (free) or apify (paid)")
@click.option("--reviews", "-r", default=5, help="Reviews per place (0=skip)")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def scrape_maps(query: str, location: str | None, max_results: int, scraper: str, reviews: int, apify_token: str | None) -> None:
    """Scrape Google Maps for business leads."""
    from suplex.config import Config
    from suplex.scraper.coordinator import ScrapeCoordinator

    token = apify_token or Config.load().apify_token
    store = LeadStore()
    coord = ScrapeCoordinator(store=store, apify_token=token, scraper=scraper, reviews_per_place=reviews)
    target = ScrapeTarget(
        source=LeadSource.GOOGLE_MAPS,
        query=query,
        location=location,
        max_results=max_results,
    )
    job_id = os.environ.get("SUPLEX_JOB_ID")
    leads = asyncio.run(coord.run([target], job_id=job_id))
    click.echo(json.dumps({"leads": len(leads)}, indent=2))


@scrape.command("linkedin")
@click.argument("query")
@click.option("--location", "-l", default=None, help="Location (city, state, or country — auto-tiled)")
@click.option("--titles", "-t", default=None, help="Job titles (comma-separated)")
@click.option("--companies", "-c", default=None, help="Current companies (comma-separated LinkedIn URLs)")
@click.option("--industries", default=None, help="Industry IDs (comma-separated)")
@click.option("--mode", "-m", type=click.Choice(["short", "full", "full_with_email"]), default="full_with_email")
@click.option("--max-results", "-n", default=0, help="Max results (0 = exhaust)")
@click.option("--apify-token", envvar="APIFY_TOKEN", default=None)
def scrape_linkedin(
    query: str, location: str | None, titles: str | None, companies: str | None,
    industries: str | None, mode: str, max_results: int, apify_token: str | None,
) -> None:
    """Scrape LinkedIn for people leads."""
    from suplex.config import Config
    from suplex.scraper.coordinator import ScrapeCoordinator

    token = apify_token or Config.load().apify_token
    store = LeadStore()
    coord = ScrapeCoordinator(store=store, apify_token=token)

    extra: dict = {"mode": mode}
    if titles:
        extra["job_titles"] = [t.strip() for t in titles.split(",")]
    if companies:
        extra["current_companies"] = [c.strip() for c in companies.split(",")]
    if industries:
        extra["industries"] = [i.strip() for i in industries.split(",")]

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
@click.option("--scraper", "-s", type=click.Choice(["auto", "playwright", "apify"]), default="auto")
def run_scrape_maps(query: str, location: str, max_results: int, scraper: str) -> None:
    """Launch Google Maps scrape in background."""
    from suplex.jobs import launch_scrape_job
    extra = []
    if max_results > 0:
        extra.extend(["-n", str(max_results)])
    if scraper != "auto":
        extra.extend(["-s", scraper])
    result = launch_scrape_job("maps", query, location, max_results=max_results, extra_args=extra)
    click.echo(json.dumps(result, indent=2))


@run.command("scrape-linkedin")
@click.argument("query")
@click.option("--location", "-l", required=True, help="Location")
@click.option("--titles", "-t", default=None, help="Job titles (comma-separated)")
def run_scrape_linkedin(query: str, location: str, titles: str | None) -> None:
    """Launch LinkedIn scrape in background."""
    from suplex.jobs import launch_scrape_job
    extra = []
    if titles:
        extra.extend(["-t", titles])
    result = launch_scrape_job("linkedin", query, location, extra_args=extra)
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


# ---------------------------------------------------------------------------
# serve (MCP server)
# ---------------------------------------------------------------------------


@cli.command()
def serve() -> None:
    """Start the MCP server (stdio transport) for AI agents."""
    from suplex.mcp_server import main as mcp_main

    mcp_main()


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------


@cli.command()
@click.argument("domain")
@click.option("--cron", "-c", default="0 9 * * 1", help="Cron expression (default: Monday 9am)")
@click.option("--preferences", "-p", default="", help="Target preferences")
def schedule(domain: str, cron: str, preferences: str) -> None:
    """Schedule recurring autonomous scrape runs."""
    from suplex.scheduler.cron import start_scheduler

    click.echo(f"Scheduling scrape for '{domain}' at cron '{cron}'", err=True)
    start_scheduler(domain=domain, preferences=preferences, cron_expr=cron)
