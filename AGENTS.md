# Suplex

Agent-driven lead scraping, enrichment, and email verification CLI + MCP tool.

## Commands

```bash
# Install
uv sync && uv run playwright install chromium

# Setup
suplex setup          # configure API keys
suplex profile <url>  # set sender profile from your website
suplex doctor         # verify services
suplex budget         # check Apify credit usage

# Scrape (foreground)
suplex scrape maps "query" -l "location" -s playwright|apify -r 5
suplex scrape linkedin "query" -l "location" -t "titles" -m full_with_email
suplex scrape website https://example.com

# Scrape (background)
suplex run scrape-maps "query" -l "location" -s playwright
suplex run scrape-linkedin "query" -l "location" -t "titles"
suplex run enrich

# Enrich
suplex enrich --linkedin-posts 1

# Monitor
suplex jobs                    # list all jobs
suplex job <id>                # check progress
suplex leads --source google_maps --status valid
suplex stats
suplex budget

# Verify
suplex verify -n 50

# MCP server
suplex serve
```

## Code Style

- Python 3.12+, type hints, async-first (`asyncio` / `anyio`)
- `uv` for package management, `ruff` for linting
- Tests: `uv run pytest`

## Boundaries

- NEVER commit API keys or tokens
- NEVER modify geo data files manually (`us-cities.json`, `worldcities.csv`)
- NEVER modify SQLite schema without migration logic for existing DBs
- All scraper output goes through `LeadStore.insert_lead()` for dedup
- Background jobs MUST pass `SUPLEX_JOB_ID` env var for progress tracking
- Apify budget is enforced — over-budget launches are blocked
- Playwright daily limit: 500 place visits/day
- LinkedIn emails are pre-verified by Apify — do NOT re-verify with Reacher

## Data Model

- **leads**: all scraped contacts with dedup (linkedin_url → google_maps_url → email+source → name+location+source)
- **sender_profile**: user's business identity (single row)
- **jobs**: background job tracking with parent/child batches
- **item_tasks**: per-tile crash recovery (pending → processing → done)
- **cities**: 59K cities across 241 countries for Google Maps geo tiling
- **linkedin_locations**: 17K cached autocomplete strings
- **linkedin_geo_map**: 316K lookup keys for LinkedIn tile resolution

## Architecture

- Scraping and enrichment are decoupled. Scrape first, enrich later.
- Google Maps Playwright: coordinate-based viewport search, 500/day limit, reviews via RPC API
- Google Maps Apify: polygon-based, budget-enforced, no daily limit
- LinkedIn Apify: autocomplete-based tiling, full+email mode, pre-verified emails
- Email verification: Reacher Docker (auto-installed, free SMTP handshake)
- Site scraping: httpx + selectolax + trafilatura (fast) → Playwright fallback for JS shells
- The agent IS the AI layer. Suplex returns clean_text + raw_data; the agent interprets it.
