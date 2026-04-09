# Suplex

Agent-driven lead scraping, enrichment, and email verification CLI + MCP tool.

## Commands

```bash
# Setup
suplex setup          # install skill, MCP server, Playwright, API keys
suplex doctor         # check services
suplex budget         # check Apify credits

# Discover locations
suplex regions Japan                   # list regions with city counts
suplex regions --search Rotterdam      # find region + country + LinkedIn autocompletes
suplex regions --search "North Holland"  # find local name (Noord-Holland)

# Scrape (foreground, auto-enriches by default)
suplex scrape maps "query" -l "location" -s playwright|apify -n 20 -r 5
suplex scrape maps "query" -l US --only CA,TX,NY -n 20
suplex scrape maps "query" -l US --exclude WY,VT -n 20
suplex scrape linkedin "query AND keyword" -l "location" --seniority 320 --functions 11 -n 20
suplex scrape linkedin "query" -l "location" -t "Specific Title" --headcount A,B -n 20
suplex scrape website https://example.com

# Scrape (background, auto-enriches by default)
suplex run scrape-maps "query" -l "location" -s playwright -n 20
suplex run scrape-linkedin "query AND keyword" -l "location" --seniority 320 --functions 11 -n 20
suplex run enrich

# Enrich (standalone)
suplex enrich --linkedin-posts 1

# Monitor + Resume
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
- **Always pass `-n` for max results.** Never exhaust without user confirmation.
- Apify budget is enforced — over-budget launches are blocked
- Playwright: 500 places/day limit, human-like pacing (2-15s delays)
- LinkedIn emails are pre-verified by Apify — do NOT re-verify with Reacher
- LinkedIn posts only fetched for leads WITH email — don't waste credits on unreachable leads
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
- LinkedIn query supports boolean operators (AND, OR, NOT, "quotes", parens) — use them.
- LinkedIn filters: --seniority, --functions, --industries, --headcount, -t, --companies, etc.
- For owners: `--seniority 320`. For healthcare: `--functions 11`. For small biz: `--headcount A,B`.
- Never use generic titles alone (-t "Owner") — combine with seniority/function filters.
- Batch jobs: parent/child in SQLite, watchdog auto-advances when slots open. No polling needed.
- Resume: `suplex resume <id>` — done tiles skipped, failed tiles retried, exact flags preserved.
- `--only` / `--exclude`: filter tiles by region. Accent-insensitive, works globally. Agent checks `suplex jobs` for completed regions, excludes them on next run.
- `suplex regions`: discover local region names before filtering. Search by city/region/country.
- The agent IS the AI layer. Suplex returns raw_data + clean_text; agent interprets.
