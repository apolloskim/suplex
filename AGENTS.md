# Suplex

Agent-driven lead scraping, enrichment, and email verification CLI + MCP tool.

## Commands

```bash
# Install
uv sync && uv run playwright install chromium

# First-time setup
uv run suplex setup          # configure API keys (interactive)
uv run suplex profile <url>  # set sender profile from your website
uv run suplex doctor         # verify all services healthy

# Scrape
uv run suplex scrape maps "query" -l "location" -s playwright|apify
uv run suplex scrape linkedin "query" -l "location" -t "titles"
uv run suplex scrape website https://example.com

# Enrich (runs on all unenriched leads)
uv run suplex enrich --linkedin-posts 1

# Background jobs
uv run suplex run scrape-maps "query" -l "location"
uv run suplex run scrape-linkedin "query" -l "location"
uv run suplex run enrich
uv run suplex jobs                    # list all jobs
uv run suplex job <id>                # check progress

# Query
uv run suplex leads --source google_maps --status valid
uv run suplex stats
uv run suplex verify -n 50

# MCP server
uv run suplex serve
```

## Code Style

- Python 3.12+, type hints required
- Async-first: use `asyncio` / `anyio` for all I/O
- `uv` for package management, never `pip`
- `ruff` for linting
- Tests: `uv run pytest`, not `uv test`

## Boundaries

- NEVER commit API keys or tokens
- NEVER modify `src/suplex/geo/us-cities.json` or `worldcities.csv` manually
- NEVER modify the SQLite schema without adding migration logic for existing DBs
- All scraper output goes through `LeadStore.insert_lead()` for dedup
- Background jobs MUST pass `SUPLEX_JOB_ID` env var for progress tracking

## Data Model

- **leads** table: all scraped contacts (Google Maps + LinkedIn + website)
- **sender_profile** table: user's own business identity (single row)
- **jobs** table: background job tracking with parent/child batches
- **item_tasks** table: per-tile crash recovery
- **cities** table: 59K cities across 241 countries for geo tiling
- **linkedin_locations** table: 17K cached autocomplete strings
- **linkedin_geo_map** table: 316K lookup keys for tile resolution

## Key Architecture Decisions

- Scraping and enrichment are decoupled. Scrape first, enrich later.
- Google Maps: Playwright (free, coordinate-based viewport search) or Apify (paid, polygon-based). Agent chooses.
- LinkedIn: Apify only. One actor run per autocomplete string. No tiling overlap.
- Email verification: Reacher Docker (auto-installed, free SMTP handshake).
- Site scraping: httpx + selectolax + trafilatura (fast) with Playwright fallback for JS shells.
- The agent IS the AI layer. Suplex returns clean_text; the agent interprets it. No embedded LLM calls.
