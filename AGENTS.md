# Suplex

Agent-driven lead scraping, enrichment, and email verification CLI + MCP tool.

## Commands

```bash
# Setup
suplex setup          # install skill, MCP server, Playwright, API keys
suplex doctor         # check services
suplex budget         # check Apify credits

# Scrape (foreground, auto-enriches by default)
suplex scrape maps "query" -l "location" -s playwright|apify -n 20 -r 5
suplex scrape maps "query" -l "location" --no-enrich
suplex scrape linkedin "query" -l "location" -t "titles" -n 20
suplex scrape website https://example.com

# Scrape (background, auto-enriches by default)
suplex run scrape-maps "query" -l "location" -s playwright -n 20
suplex run scrape-linkedin "query" -l "location" -n 20 -t "titles"
suplex run enrich

# Enrich (standalone)
suplex enrich --linkedin-posts 1

# Monitor
suplex jobs
suplex job <id>
suplex resume <id>     # pick up where a killed/failed job left off

# Query + Export
suplex leads --source google_maps --status valid
suplex stats
suplex export -o leads.csv
suplex export -s linkedin -v valid -o verified_linkedin.csv

# Other
suplex profile <url>   # sender profile
suplex verify -n 50    # standalone email verification
suplex serve           # MCP server (stdio)
```

## Boundaries

- NEVER commit API keys or tokens
- NEVER modify geo data files manually
- All scraper output goes through `LeadStore.insert_lead()` for dedup
- Apify budget is enforced — over-budget launches are blocked
- Playwright: 500 places/day limit, human-like pacing
- LinkedIn emails are pre-verified by Apify — do NOT re-verify with Reacher
- Scraping auto-enriches by default — use `--no-enrich` to skip

## Data Model

- **leads**: dedup by linkedin_url → google_maps_url → email+source → name+location+source
- **jobs**: background job tracking with parent/child batches, auto-advancing watchdog
- **item_tasks**: per-tile crash recovery (pending → processing → done/failed)
- **cities**: 59K cities across 241 countries
- **linkedin_geo_map**: 416K lookup keys for tile resolution
- **sender_profile**: user's business identity (single row)

## Architecture

- Scraping auto-enriches by default (--enrich flag, on by default)
- Google Maps: Playwright (free, 500/day) or Apify (paid, budget-enforced). Reviews via RPC API.
- LinkedIn: Apify only. Full+email mode. Pre-verified emails. All SDR signals stored.
- Batch jobs: parent/child in SQLite, watchdog auto-advances when slots open
- Resume: `suplex resume <id>` — done tiles skipped, failed tiles retried
- Email: site-extracted first, then guessing (role addresses for businesses, permutations for persons). Catch-all domains: site emails kept, guesses rejected.
- The agent IS the AI layer. Suplex returns raw_data + clean_text; agent interprets.
