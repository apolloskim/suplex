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
                "Scrape Google Maps for business leads in the foreground with the local "
                "Playwright scraper (free). Returns business name, phone, address, website. "
                "For the paid Apify scraper or large areas, use run_background_job."
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
                "Query supports boolean operators: AND, OR, NOT, \"quotes\", (parens). "
                "Use filters to narrow results precisely. Always pass max_results."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "LinkedIn search query. Supports boolean operators: "
                            "AND, OR, NOT, \"exact phrase\", (grouping). "
                            "Example: 'dentist AND owner', 'CEO NOT (assistant OR SVP)'"
                        ),
                    },
                    "location": {
                        "type": "string",
                        "description": "Location (city, state, or country — auto-tiled)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum results to return. ALWAYS set this.",
                        "default": 50,
                    },
                    "job_titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Current job titles filter (exact match). Example: ['Dental Practice Owner', 'Dentist']",
                    },
                    "past_job_titles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Past job titles filter (exact match)",
                    },
                    "seniority_levels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Seniority level IDs. "
                            "100=In Training, 110=Entry, 120=Senior, 130=Strategic, "
                            "200=Entry Manager, 210=Experienced Manager, 220=Director, "
                            "300=VP, 310=CXO, 320=Owner/Partner"
                        ),
                    },
                    "function_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Function IDs. "
                            "1=Accounting, 4=BizDev, 7=Education, 8=Engineering, "
                            "9=Entrepreneurship, 10=Finance, 11=Healthcare Services, "
                            "13=IT, 14=Legal, 15=Marketing, 18=Operations, 25=Sales"
                        ),
                    },
                    "industry_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Industry IDs (numeric). See: "
                            "github.com/HarvestAPI/linkedin-industry-codes-v2"
                        ),
                    },
                    "company_headcount": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Company size codes. "
                            "A=Self-employed, B=1-10, C=11-50, D=51-200, "
                            "E=201-500, F=501-1K, G=1K-5K, H=5K-10K, I=10K+"
                        ),
                    },
                    "current_companies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Current companies (LinkedIn company URLs)",
                    },
                    "past_companies": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Past companies (LinkedIn company URLs)",
                    },
                    "schools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Schools filter",
                    },
                    "years_experience": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Years of experience IDs. 1=<1yr, 2=1-2, 3=3-5, 4=6-10, 5=10+",
                    },
                    "years_at_company": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Years at current company IDs. Same scale as years_experience.",
                    },
                    "company_hq_locations": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Company HQ locations filter",
                    },
                    "profile_languages": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Profile languages (e.g. English, Spanish, French)",
                    },
                    "recently_changed_jobs": {
                        "type": "boolean",
                        "description": "Only people who recently changed jobs",
                    },
                    "only_regions": {
                        "type": "string",
                        "description": "Only include these regions (comma-separated, e.g. CA,TX)",
                    },
                    "exclude_regions": {
                        "type": "string",
                        "description": "Exclude these regions (comma-separated)",
                    },
                },
                "required": ["query", "max_results"],
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
            name="enrich_leads",
            description=(
                "Enrich unenriched leads. For each lead: scrapes their website for rich "
                "business data, verifies their email, guesses email if missing (name+domain "
                "permutations), and optionally fetches latest LinkedIn posts. "
                "Runs independently from scraping — picks up all unenriched leads."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max leads to enrich in this batch",
                        "default": 100,
                    },
                    "linkedin_posts": {
                        "type": "integer",
                        "description": "Number of LinkedIn posts to fetch per lead (0=skip)",
                        "default": 1,
                    },
                },
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
        Tool(
            name="run_background_job",
            description=(
                "Launch a scrape or enrich job in the background. Returns immediately with "
                "a job_id. The job runs as a detached process — survives terminal closure. "
                "Use get_job_status to check progress."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["scrape_maps", "scrape_linkedin", "enrich"],
                        "description": "Job type",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query (for scrape jobs)",
                    },
                    "location": {
                        "type": "string",
                        "description": "Location (for scrape jobs) — city, state, or country",
                    },
                    "titles": {
                        "type": "string",
                        "description": "Current job titles, comma-separated (LinkedIn)",
                    },
                    "past_titles": {"type": "string", "description": "Past job titles, comma-separated (LinkedIn)"},
                    "companies": {"type": "string", "description": "Current companies, comma-separated (LinkedIn)"},
                    "past_companies": {"type": "string", "description": "Past companies, comma-separated (LinkedIn)"},
                    "industries": {"type": "string", "description": "Industry IDs, comma-separated (LinkedIn)"},
                    "seniority": {"type": "string", "description": "Seniority IDs (320=Owner/Partner,310=CXO) (LinkedIn)"},
                    "functions": {"type": "string", "description": "Function IDs (11=Healthcare,8=Engineering) (LinkedIn)"},
                    "headcount": {"type": "string", "description": "Company headcount codes (A=Self-employed,B=1-10) (LinkedIn)"},
                    "schools": {"type": "string", "description": "Schools, comma-separated (LinkedIn)"},
                    "years_exp": {"type": "string", "description": "Years experience IDs 1-5 (LinkedIn)"},
                    "years_at_company": {"type": "string", "description": "Years at company IDs 1-5 (LinkedIn)"},
                    "company_hq": {"type": "string", "description": "Company HQ locations (LinkedIn)"},
                    "profile_languages": {"type": "string", "description": "Profile languages (LinkedIn)"},
                    "recently_changed_jobs": {"type": "boolean", "description": "Recently changed jobs (LinkedIn)"},
                    "max_results": {"type": "integer", "description": "Max results (for scrape jobs)"},
                    "only": {"type": "string", "description": "Only these regions, comma-separated"},
                    "exclude": {"type": "string", "description": "Exclude these regions, comma-separated"},
                    "limit": {
                        "type": "integer",
                        "description": "Max leads (for enrich jobs)",
                        "default": 100,
                    },
                    "linkedin_posts": {
                        "type": "integer",
                        "description": "LinkedIn posts per lead (for enrich jobs)",
                        "default": 1,
                    },
                    "scraper": {
                        "type": "string",
                        "enum": ["auto", "playwright", "apify"],
                        "description": "For Google Maps: playwright (free, slower) or apify (paid, faster). Default: auto.",
                        "default": "auto",
                    },
                },
                "required": ["type"],
            },
        ),
        Tool(
            name="get_job_status",
            description="Check progress of a background job. Returns completed/total counts and status.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {
                        "type": "string",
                        "description": "The job ID returned by run_background_job",
                    },
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="run_batch_job",
            description=(
                "Launch multiple scrape/enrich jobs as a batch. The agent decomposes "
                "a complex request (e.g., 'scrape dental hygienists, dentist owners, "
                "pediatric dentists across LinkedIn and Google Maps') into individual "
                "jobs. Returns a batch_id for tracking. Use get_batch_status to monitor."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "jobs": {
                        "type": "array",
                        "description": "List of job specs to run",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": ["scrape_maps", "scrape_linkedin", "enrich"]},
                                "query": {"type": "string"},
                                "location": {"type": "string"},
                                "max_results": {"type": "integer"},
                                "scraper": {"type": "string", "enum": ["auto", "playwright", "apify"]},
                                "titles": {"type": "string"},
                                "past_titles": {"type": "string"},
                                "companies": {"type": "string"},
                                "past_companies": {"type": "string"},
                                "industries": {"type": "string"},
                                "seniority": {"type": "string"},
                                "functions": {"type": "string"},
                                "headcount": {"type": "string"},
                                "schools": {"type": "string"},
                                "years_exp": {"type": "string"},
                                "years_at_company": {"type": "string"},
                                "company_hq": {"type": "string"},
                                "profile_languages": {"type": "string"},
                                "recently_changed_jobs": {"type": "boolean"},
                                "only": {"type": "string"},
                                "exclude": {"type": "string"},
                                "limit": {"type": "integer", "description": "Max leads (enrich jobs)"},
                                "linkedin_posts": {"type": "integer", "description": "Posts per lead (enrich jobs)"},
                            },
                            "required": ["type"],
                        },
                    },
                    "max_parallel": {
                        "type": "integer",
                        "description": "Max jobs running at once",
                        "default": 3,
                    },
                },
                "required": ["jobs"],
            },
        ),
        Tool(
            name="get_batch_status",
            description="Check progress of a batch job. Shows each child job's status and total leads.",
            inputSchema={
                "type": "object",
                "properties": {
                    "batch_id": {"type": "string", "description": "Batch ID from run_batch_job"},
                },
                "required": ["batch_id"],
            },
        ),
        Tool(
            name="advance_batch",
            description=(
                "Check a batch and launch next pending jobs if slots are open. "
                "Call this periodically to keep the batch moving."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "batch_id": {"type": "string"},
                    "max_parallel": {"type": "integer", "default": 3},
                },
                "required": ["batch_id"],
            },
        ),
        Tool(
            name="resume_job",
            description=(
                "Resume a stopped or failed job. Picks up where it left off — "
                "already-done tiles are skipped, failed tiles are retried."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "Job ID to resume"},
                },
                "required": ["job_id"],
            },
        ),
        Tool(
            name="list_jobs",
            description="List recent background jobs with their status and progress.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["running", "done", "done_with_errors", "failed", "pending", "stopped"],
                        "description": "Filter by status",
                    },
                },
            },
        ),
        Tool(
            name="get_apify_budget",
            description=(
                "Check Apify credit usage and remaining budget. "
                "Call this BEFORE launching Apify jobs (LinkedIn, Google Maps with apify scraper) "
                "to ensure there's enough budget. Returns used/remaining USD and active runs."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="estimate_job_cost",
            description=(
                "Estimate cost of a scrape job before running it. "
                "Returns estimated pages, profiles, and USD cost."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["linkedin", "google_maps"],
                    },
                    "tile_count": {
                        "type": "integer",
                        "description": "Number of tiles (from resolve_tiles or location lookup)",
                    },
                    "results_per_tile": {
                        "type": "integer",
                        "description": "Estimated results per tile",
                        "default": 25,
                    },
                },
                "required": ["type", "tile_count"],
            },
        ),
        # --- Outreach tools ---
        Tool(
            name="get_outreach_playbook",
            description=(
                "Get the outreach playbook (tone, angle, CTA, footer, audience). "
                "If no playbook exists, tell the user and offer to create one. "
                "Before every outreach run, show the summary and ask if they want to adjust."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="update_outreach_playbook",
            description="Update the outreach playbook with new content.",
            inputSchema={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Full markdown content for the playbook"},
                },
                "required": ["content"],
            },
        ),
        Tool(
            name="list_mailboxes",
            description="List registered sender mailboxes with today's send count and daily limit.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="add_mailbox",
            description="Register a new Gmail mailbox for outreach. Returns auth instructions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "Gmail address"},
                    "display_name": {"type": "string", "description": "Sender display name"},
                    "daily_limit": {"type": "integer", "description": "Max emails per day", "default": 10},
                },
                "required": ["email"],
            },
        ),
        Tool(
            name="create_campaign",
            description=(
                "Create an outreach campaign. Defines who to email (filters), "
                "follow-up cadence, and sender mailbox. Requires a physical address (CAN-SPAM)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Campaign name"},
                    "mailbox_id": {"type": "string", "description": "Sender mailbox ID"},
                    "physical_address": {"type": "string", "description": "Physical postal address (CAN-SPAM required)"},
                    "source_filter": {"type": "string", "description": "Lead source: google_maps, linkedin"},
                    "enriched_only": {"type": "boolean", "description": "Only enriched leads", "default": True},
                    "cadence": {
                        "type": "array",
                        "description": "Follow-up cadence. Default: step 1 day 0, step 2 day 3, step 3 day 7",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "integer"},
                                "delay_days": {"type": "integer"},
                            },
                        },
                    },
                },
                "required": ["name", "mailbox_id", "physical_address"],
            },
        ),
        Tool(
            name="preview_outreach",
            description=(
                "Get eligible leads + playbook for drafting. Returns lead data and playbook "
                "so the agent can generate personalized emails. Always show playbook summary "
                "and ask user before drafting."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 5},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="save_outreach_drafts",
            description="Save agent-generated email drafts to the outreach table.",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                    "mailbox_id": {"type": "string"},
                    "drafts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "lead_id": {"type": "integer"},
                                "email": {"type": "string"},
                                "subject": {"type": "string"},
                                "body": {"type": "string"},
                                "step": {"type": "integer", "default": 1},
                            },
                            "required": ["lead_id", "subject", "body"],
                        },
                    },
                },
                "required": ["campaign_id", "mailbox_id", "drafts"],
            },
        ),
        Tool(
            name="approve_outreach_drafts",
            description="Approve drafts for sending. Use outreach_ids for specific drafts, or campaign_id with approve_all.",
            inputSchema={
                "type": "object",
                "properties": {
                    "outreach_ids": {"type": "array", "items": {"type": "string"}},
                    "campaign_id": {"type": "string", "description": "Approve all drafts in this campaign"},
                    "approve_all": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="send_outreach",
            description=(
                "Send approved outreach emails via gws Gmail. Respects daily limits per mailbox "
                "and checks suppression list. Never send without user reviewing drafts first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max emails to send", "default": 10},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="check_outreach_replies",
            description="Check Gmail for replies to sent outreach. Auto-detects opt-outs and adds to suppression list.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mailbox_id": {"type": "string"},
                },
                "required": ["mailbox_id"],
            },
        ),
        Tool(
            name="get_due_followups",
            description="Get leads due for follow-up based on campaign cadence. Agent drafts follow-up copy.",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                },
                "required": ["campaign_id"],
            },
        ),
        Tool(
            name="get_outreach_stats",
            description="Get outreach stats for a campaign: drafted, approved, sent, replied, bounced counts.",
            inputSchema={
                "type": "object",
                "properties": {
                    "campaign_id": {"type": "string"},
                },
                "required": ["campaign_id"],
            },
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
        return {"count": len(leads), "leads": [lead.model_dump() for lead in leads[:20]]}

    elif name == "scrape_linkedin":
        extra: dict = {"mode": "full_with_email"}
        # Map MCP args to extra dict keys that _build_input() reads.
        _EXTRA_MAP = {
            "job_titles": "job_titles",
            "past_job_titles": "past_job_titles",
            "seniority_levels": "seniority_levels",
            "function_ids": "functions",
            "industry_ids": "industries",
            "company_headcount": "company_headcount",
            "current_companies": "current_companies",
            "past_companies": "past_companies",
            "schools": "schools",
            "years_experience": "years_experience",
            "years_at_company": "years_at_company",
            "company_hq_locations": "company_hq_locations",
            "profile_languages": "profile_languages",
        }
        for mcp_key, extra_key in _EXTRA_MAP.items():
            if args.get(mcp_key):
                extra[extra_key] = args[mcp_key]
        if args.get("recently_changed_jobs"):
            extra["recently_changed_jobs"] = True
        if args.get("only_regions"):
            extra["only_states"] = [s.strip().upper() for s in args["only_regions"].split(",")]
        if args.get("exclude_regions"):
            extra["exclude_states"] = [s.strip().upper() for s in args["exclude_regions"].split(",")]
        target = ScrapeTarget(
            source=LeadSource.LINKEDIN,
            query=args["query"],
            location=args.get("location"),
            max_results=args.get("max_results", 50),
            extra=extra,
        )
        leads = await coordinator.run([target])
        return {"count": len(leads), "leads": [lead.model_dump() for lead in leads[:20]]}

    elif name == "scrape_website":
        target = ScrapeTarget(source=LeadSource.PLAYWRIGHT, query=args["url"])
        leads = await coordinator.run([target])
        return {"count": len(leads), "leads": [lead.model_dump() for lead in leads]}

    elif name == "run_scrape_plan":
        plan = await strategist.plan(
            domain=args["domain"],
            preferences=args.get("preferences", ""),
        )
        leads = await coordinator.run(plan.targets)
        return {
            "plan": plan.model_dump(),
            "leads_scraped": len(leads),
            "leads_preview": [lead.model_dump() for lead in leads[:10]],
        }

    elif name == "enrich_leads":
        from suplex.enrich.pipeline import EnrichmentPipeline
        config = Config.load()
        pipeline = EnrichmentPipeline(
            store=store,
            apify_token=config.apify_token,
            linkedin_posts=args.get("linkedin_posts", 1),
        )
        return await pipeline.run(limit=args.get("limit", 100))

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
        return {"count": len(leads), "leads": [lead.model_dump() for lead in leads]}

    elif name == "get_stats":
        stats = {}
        for src in LeadSource:
            for vs in VerificationStatus:
                count = store.count(source=src, verification_status=vs)
                if count > 0:
                    stats[f"{src.value}/{vs.value}"] = count
        stats["total"] = store.count()
        return stats

    elif name == "run_background_job":
        from suplex.jobs import launch_scrape_job, launch_enrich_job
        job_type = args["type"]
        if job_type == "scrape_maps":
            extra = []
            scraper = args.get("scraper", "auto")
            if scraper != "auto":
                extra.extend(["-s", scraper])
            if args.get("max_results"):
                extra.extend(["-n", str(args["max_results"])])
            if args.get("only"):
                extra.extend(["--only", args["only"]])
            if args.get("exclude"):
                extra.extend(["--exclude", args["exclude"]])
            return launch_scrape_job("maps", args.get("query", ""), args.get("location", ""), extra_args=extra)
        elif job_type == "scrape_linkedin":
            extra = []
            _BG_FLAG_MAP = {
                "titles": "-t", "past_titles": "--past-titles",
                "companies": "-c", "past_companies": "--past-companies",
                "industries": "--industries", "seniority": "--seniority",
                "functions": "--functions", "headcount": "--headcount",
                "schools": "--schools", "years_exp": "--years-exp",
                "years_at_company": "--years-at-company",
                "company_hq": "--company-hq",
                "profile_languages": "--profile-languages",
            }
            for key, flag in _BG_FLAG_MAP.items():
                if args.get(key):
                    extra.extend([flag, args[key]])
            if args.get("recently_changed_jobs"):
                extra.append("--recently-changed-jobs")
            if args.get("max_results"):
                extra.extend(["-n", str(args["max_results"])])
            if args.get("only"):
                extra.extend(["--only", args["only"]])
            if args.get("exclude"):
                extra.extend(["--exclude", args["exclude"]])
            return launch_scrape_job("linkedin", args.get("query", ""), args.get("location", ""), extra_args=extra)
        elif job_type == "enrich":
            return launch_enrich_job(limit=args.get("limit", 100), linkedin_posts=args.get("linkedin_posts", 1))
        else:
            raise ValueError(f"Unknown job type: {job_type}")

    elif name == "run_batch_job":
        from suplex.jobs import launch_batch_job
        return launch_batch_job(args["jobs"], max_parallel=args.get("max_parallel", 3))

    elif name == "get_batch_status":
        from suplex.jobs import get_batch_status as _get_batch
        return _get_batch(args["batch_id"])

    elif name == "advance_batch":
        from suplex.jobs import advance_batch as _advance
        return _advance(args["batch_id"], max_parallel=args.get("max_parallel", 3))

    elif name == "get_job_status":
        from suplex.jobs import get_job_status as _get_status
        return _get_status(args["job_id"])

    elif name == "resume_job":
        from suplex.jobs import resume_job
        return resume_job(args["job_id"])

    elif name == "list_jobs":
        from suplex.jobs import list_jobs as _list
        return _list(status=args.get("status"))

    elif name == "get_apify_budget":
        from suplex.apify_budget import get_apify_budget
        return await get_apify_budget()

    elif name == "estimate_job_cost":
        from suplex.apify_budget import estimate_linkedin_cost, estimate_gmaps_cost
        if args["type"] == "linkedin":
            return estimate_linkedin_cost(args["tile_count"], args.get("results_per_tile", 25))
        else:
            return estimate_gmaps_cost(args["tile_count"], args.get("results_per_tile", 100))

    # --- Outreach tools ---

    elif name == "get_outreach_playbook":
        from suplex.outreach.playbook import get_playbook, get_playbook_summary, playbook_exists
        if not playbook_exists():
            return {"exists": False, "message": "No playbook yet. Walk the user through creating one."}
        return {"exists": True, "summary": get_playbook_summary(), "content": get_playbook()}

    elif name == "update_outreach_playbook":
        from suplex.outreach.playbook import update_playbook
        path = update_playbook(args["content"])
        return {"updated": True, "path": str(path)}

    elif name == "list_mailboxes":
        from suplex.outreach.mailbox import list_mailboxes
        return {"mailboxes": list_mailboxes()}

    elif name == "add_mailbox":
        from suplex.outreach.mailbox import add_mailbox
        return add_mailbox(args["email"], args.get("display_name", ""), args.get("daily_limit", 10))

    elif name == "create_campaign":
        import uuid as _uuid
        campaign_id = _uuid.uuid4().hex[:12]
        filter_json = {}
        if args.get("source_filter"):
            filter_json["source"] = args["source_filter"]
        if args.get("enriched_only", True):
            filter_json["enriched_only"] = True
        cadence = args.get("cadence", [
            {"step": 1, "delay_days": 0},
            {"step": 2, "delay_days": 3},
            {"step": 3, "delay_days": 7},
        ])
        store.create_campaign(campaign_id, args["name"], filter_json, cadence,
                              args["mailbox_id"], args["physical_address"],
                              args.get("playbook_override"))
        return {"campaign_id": campaign_id, "name": args["name"]}

    elif name == "preview_outreach":
        from suplex.outreach.engine import preview_context
        return preview_context(args["campaign_id"], limit=args.get("limit", 5), store=store)

    elif name == "save_outreach_drafts":
        from suplex.outreach.engine import save_drafts
        return {"saved": save_drafts(args["drafts"], args["campaign_id"], args["mailbox_id"], store=store)}

    elif name == "approve_outreach_drafts":
        from suplex.outreach.engine import approve_drafts, approve_all_drafts
        if args.get("approve_all") and args.get("campaign_id"):
            return {"approved": approve_all_drafts(args["campaign_id"], store=store)}
        elif args.get("outreach_ids"):
            return {"approved": approve_drafts(args["outreach_ids"], store=store)}
        return {"error": "Provide outreach_ids or campaign_id with approve_all"}

    elif name == "send_outreach":
        from suplex.outreach.engine import send_approved
        return send_approved(args["campaign_id"], limit=args.get("limit", 10), store=store)

    elif name == "check_outreach_replies":
        from suplex.outreach.engine import check_replies
        return check_replies(args["mailbox_id"], store=store)

    elif name == "get_due_followups":
        from suplex.outreach.engine import get_due_followups
        return {"due": get_due_followups(args["campaign_id"], store=store)}

    elif name == "get_outreach_stats":
        return store.get_outreach_stats(args["campaign_id"])

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
