"""MCP server — exposes scraping, verification, and planning tools for AI agents."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from suplex.ai.strategist import Strategist
from suplex.config import Config
from suplex.models import LeadSource, ScrapeTarget, VerificationStatus
from suplex.profile import SenderProfile, scrape_sender_profile
from suplex.scraper.coordinator import ScrapeCoordinator
from suplex.setup import SERVICES, configure_key, get_setup_status
from suplex.store.db import LeadStore
from suplex.verify.verifier import EmailVerifier

logger = logging.getLogger(__name__)

server = Server("suplex")


def _load_services() -> tuple[LeadStore, ScrapeCoordinator, EmailVerifier, Strategist]:
    config = Config.load()
    store = LeadStore()
    coordinator = ScrapeCoordinator(store=store, apify_token=config.apify_token)
    verifier = EmailVerifier(
        reacher_api_key=config.reacher_api_key,
        million_verifier_key=config.million_verifier_api_key,
    )
    strategist = Strategist(api_key=config.anthropic_api_key)
    return store, coordinator, verifier, strategist


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="setup_status",
            description=(
                "Check which API keys are configured and which are missing. "
                "Call this FIRST before any scraping — if required keys are missing, "
                "guide the user through setup using the configure tool. "
                "Returns signup URLs and descriptions for each service."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="configure",
            description=(
                "Set an API key for a service. Validates the key against the real API "
                "before saving. Use after asking the user for their key. "
                "Keys: apify_token, anthropic_api_key, reacher_api_key, million_verifier_api_key"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "enum": list(SERVICES.keys()),
                        "description": "Which service to configure",
                    },
                    "value": {
                        "type": "string",
                        "description": "The API key/token value",
                    },
                },
                "required": ["key", "value"],
            },
        ),
        Tool(
            name="ensure_reacher",
            description=(
                "Ensure Reacher email verification is running. "
                "Checks if Docker is available, pulls the Reacher image if needed, "
                "and starts the container. Free self-hosted SMTP verification. "
                "Call this before verify_emails if setup_status shows no reacher_api_key."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_profile",
            description=(
                "Scrape the user's own website to create a sender profile. "
                "This is NOT a lead — it's the user's business identity used for "
                "email generation and scrape planning. Call during onboarding after "
                "asking for their website URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The user's business website URL",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="get_profile",
            description=(
                "Get the current sender profile. Returns the user's business identity "
                "including description, value proposition, emails, and clean text from their site."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="plan_scrape",
            description=(
                "Generate an AI-driven scrape plan based on a business domain and preferences. "
                "The AI strategist decides the best Google Maps queries, LinkedIn searches, "
                "and websites to crawl to find ideal leads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The user's business domain (e.g. 'B2B SaaS for restaurants')",
                    },
                    "preferences": {
                        "type": "string",
                        "description": "Target preferences: location, company size, titles, etc.",
                        "default": "",
                    },
                    "max_targets": {
                        "type": "integer",
                        "description": "Max number of scrape targets to generate",
                        "default": 10,
                    },
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="scrape_google_maps",
            description=(
                "Scrape Google Maps for business leads via Apify. "
                "Returns business name, email, phone, address, website."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'restaurants in Austin TX')",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location filter",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return",
                        "default": 100,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="scrape_linkedin",
            description=(
                "Scrape LinkedIn for people/leads via Apify. "
                "Returns name, title, company, location, profile URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "LinkedIn search URL or keyword query",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location filter",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return",
                        "default": 50,
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="scrape_website",
            description=(
                "Scrape a website with Playwright to extract contact info (emails, phones). "
                "Automatically finds and crawls contact/about/team pages."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Website URL to scrape",
                    },
                },
                "required": ["url"],
            },
        ),
        Tool(
            name="run_scrape_plan",
            description=(
                "Execute a full scrape plan: AI generates targets, then runs all scrapers "
                "concurrently (Playwright + Apify), stores results in local SQLite."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "The user's business domain",
                    },
                    "preferences": {
                        "type": "string",
                        "description": "Target preferences",
                        "default": "",
                    },
                },
                "required": ["domain"],
            },
        ),
        Tool(
            name="verify_emails",
            description=(
                "Verify emails in the database. Runs syntax, DNS/MX, SMTP (Reacher), "
                "and catch-all detection (MillionVerifier). Updates lead records."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max emails to verify in this batch",
                        "default": 50,
                    },
                },
            },
        ),
        Tool(
            name="get_leads",
            description="Retrieve leads from the local database with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "enum": ["google_maps", "linkedin", "playwright"],
                        "description": "Filter by lead source",
                    },
                    "verification_status": {
                        "type": "string",
                        "enum": ["pending", "valid", "invalid", "catch_all", "unknown"],
                        "description": "Filter by verification status",
                    },
                    "limit": {
                        "type": "integer",
                        "default": 100,
                    },
                },
            },
        ),
        Tool(
            name="get_stats",
            description="Get lead count statistics grouped by source and verification status.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments)
        return [TextContent(type="text", text=json.dumps(result, default=str, indent=2))]
    except Exception as e:
        logger.exception("Tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


async def _dispatch(name: str, args: dict) -> dict:
    if name == "setup_status":
        status = get_setup_status()
        status["sender_profile"] = SenderProfile.load().summary()
        return status

    elif name == "configure":
        return await configure_key(args["key"], args["value"])

    elif name == "ensure_reacher":
        return await _ensure_reacher_docker()

    elif name == "set_profile":
        profile = await scrape_sender_profile(args["url"])
        return {"saved": True, **profile.summary()}

    elif name == "get_profile":
        profile = SenderProfile.load()
        return profile.summary()

    # Reload config-dependent services on each call.
    store, coordinator, verifier, strategist = _load_services()

    if name == "plan_scrape":
        plan = await strategist.plan(
            domain=args["domain"],
            preferences=args.get("preferences", ""),
            max_targets=args.get("max_targets", 10),
        )
        return plan.model_dump()

    elif name == "scrape_google_maps":
        target = ScrapeTarget(
            source=LeadSource.GOOGLE_MAPS,
            query=args["query"],
            location=args.get("location"),
            max_results=args.get("max_results", 100),
        )
        leads = await coordinator.run([target])
        return {"count": len(leads), "leads": [l.model_dump() for l in leads[:20]]}

    elif name == "scrape_linkedin":
        target = ScrapeTarget(
            source=LeadSource.LINKEDIN,
            query=args["query"],
            location=args.get("location"),
            max_results=args.get("max_results", 50),
        )
        leads = await coordinator.run([target])
        return {"count": len(leads), "leads": [l.model_dump() for l in leads[:20]]}

    elif name == "scrape_website":
        target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=args["url"])
        leads = await coordinator.run([target])
        return {"count": len(leads), "leads": [l.model_dump() for l in leads]}

    elif name == "run_scrape_plan":
        plan = await strategist.plan(
            domain=args["domain"],
            preferences=args.get("preferences", ""),
        )
        leads = await coordinator.run(plan.targets)
        return {
            "plan": plan.model_dump(),
            "leads_scraped": len(leads),
            "leads_preview": [l.model_dump() for l in leads[:10]],
        }

    elif name == "verify_emails":
        limit = args.get("limit", 50)
        unverified = store.get_unverified_emails(limit=limit)
        results = []
        for lead in unverified:
            if lead.email and lead.id:
                vr = await verifier.verify(lead.email)
                store.update_verification(lead.id, vr.status)
                results.append(vr.model_dump())
        return {"verified": len(results), "results": results}

    elif name == "get_leads":
        source = LeadSource(args["source"]) if args.get("source") else None
        vs = VerificationStatus(args["verification_status"]) if args.get("verification_status") else None
        leads = store.get_leads(source=source, verification_status=vs, limit=args.get("limit", 100))
        return {"count": len(leads), "leads": [l.model_dump() for l in leads]}

    elif name == "get_stats":
        stats = {}
        for src in LeadSource:
            for vs in VerificationStatus:
                count = store.count(source=src, verification_status=vs)
                if count > 0:
                    stats[f"{src.value}/{vs.value}"] = count
        stats["total"] = store.count()
        return stats

    else:
        raise ValueError(f"Unknown tool: {name}")


async def _ensure_reacher_docker() -> dict:
    """Check Docker and start Reacher container if needed."""
    import shutil

    if not shutil.which("docker"):
        return {
            "status": "docker_not_found",
            "message": "Docker is not installed. Ask the user to install Docker Desktop from https://docs.docker.com/get-docker/ then try again.",
        }

    # Check if container exists and is running.
    proc = await asyncio.create_subprocess_exec(
        "docker", "inspect", "-f", "{{.State.Running}}", "suplex-reacher",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()

    if proc.returncode == 0 and b"true" in stdout:
        return {"status": "running", "message": "Reacher is already running at localhost:8080"}

    if proc.returncode == 0:
        # Exists but stopped — start it.
        proc = await asyncio.create_subprocess_exec(
            "docker", "start", "suplex-reacher",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.wait()
        return {"status": "started", "message": "Reacher container restarted at localhost:8080"}

    # Container doesn't exist — pull and create.
    proc = await asyncio.create_subprocess_exec(
        "docker", "run", "-d",
        "--name", "suplex-reacher",
        "-p", "8080:8080",
        "--restart", "unless-stopped",
        "reacherhq/backend:latest",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        return {"status": "error", "message": f"Docker run failed: {stderr.decode()}"}

    # Wait for ready.
    for _ in range(15):
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                resp = await client.get("http://localhost:8080")
                if resp.status_code in (200, 404):
                    return {"status": "created", "message": "Reacher pulled and running at localhost:8080"}
        except httpx.ConnectError:
            pass
        await asyncio.sleep(1)

    return {"status": "started_slow", "message": "Reacher container started but still warming up. Try verify_emails in a few seconds."}


async def run_server() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    asyncio.run(run_server())


if __name__ == "__main__":
    main()
