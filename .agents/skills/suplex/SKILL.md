---
name: suplex
description: >
  Scrape B2B leads from Google Maps and LinkedIn, enrich with website data
  and email verification, run background batch jobs. Use when the user asks
  to find leads, scrape contacts, build prospect lists, verify emails, or
  enrich business data. Handles geographic tiling across 241 countries.
metadata:
  author: apolloskim
  version: "0.1.0"
argument-hint: <scrape|enrich|verify|batch>
---

# Suplex Lead Scraping Skill

## First-time setup

Before any scraping, check setup:

```bash
suplex doctor
```

If API keys are missing, run `suplex setup`. Apify token is needed for LinkedIn and paid Google Maps. Playwright scraping works with zero keys.

For the sender profile (needed for email generation later):

```bash
suplex profile https://their-website.com
```

## Choosing a scraping approach

### Google Maps

Default to **Playwright** (free). Switch to **Apify** only when:
- The user explicitly asks for Apify
- You need >120 results per city (Playwright caps at ~120, Apify gets thousands)
- Speed matters more than cost

```bash
# Free (Playwright, coordinate-based search)
suplex scrape maps "dentists" -l "Texas" -s playwright

# Paid (Apify, polygon-based, more results)
suplex scrape maps "dentists" -l "Texas" -s apify
```

Geographic tiling is automatic. "Texas" expands to 1,025 city tiles. "US" expands to 10,000. "Japan" to 1,339. Each tile scrapes independently and deduplicates.

### LinkedIn

Always uses Apify (no free option). Each city's autocomplete string becomes one actor run. Full+email mode is the default.

```bash
suplex scrape linkedin "dental hygienist" -l "Texas"
suplex scrape linkedin "" -l "US" -t "Dental Office Manager,Practice Manager"
```

### Website enrichment

Site scraping is always free. Uses httpx+selectolax (fast) with Playwright fallback for JS-heavy sites.

```bash
suplex scrape website https://example.com
```

## Batch scraping workflow

When the user gives multiple queries and/or multiple sources, decompose into a batch:

**Step 1:** Break the request into individual jobs.

Example: "Scrape dental hygienists and dentist owners across LinkedIn and Google Maps in Texas"

→ 4 jobs:
- scrape_maps / "dental hygienist" / Texas
- scrape_maps / "dentist owner" / Texas
- scrape_linkedin / "dental hygienist" / Texas
- scrape_linkedin / "dentist owner" / Texas

**Step 2:** Launch as a batch via MCP or CLI.

MCP:
```json
run_batch_job({
  "jobs": [
    {"type": "scrape_maps", "query": "dental hygienist", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_maps", "query": "dentist owner", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_linkedin", "query": "dental hygienist", "location": "Texas"},
    {"type": "scrape_linkedin", "query": "dentist owner", "location": "Texas", "titles": "Owner,Founder,Practice Owner"}
  ],
  "max_parallel": 3
})
```

**Step 3:** Monitor progress.

```json
get_batch_status("batch_id")
→ {"done": 2, "running": 2, "pending": 0, "total_leads": 4231}
```

Call `advance_batch("batch_id")` periodically to launch next pending jobs when slots open.

**Step 4:** Enrich when scraping is complete.

```json
run_background_job({"type": "enrich"})
```

## Enrichment workflow

Enrichment is separate from scraping. Run it after scraping, or on a schedule.

```bash
suplex enrich                     # enrich all unenriched leads
suplex enrich --linkedin-posts 1  # default: fetch 1 latest LinkedIn post per lead
suplex enrich --linkedin-posts 0  # skip LinkedIn posts (cheaper)
```

What enrichment does per lead:
1. Scrapes their company website (free) → business data, clean_text
2. Verifies their email via Reacher Docker (free, auto-installed)
3. Guesses email if missing (name + domain permutations → verify each)
4. Fetches LinkedIn posts (Apify, ~$0.002/lead, optional)

## Email verification

Fully automatic. Reacher Docker is auto-installed on first use (installs Homebrew + colima if needed on macOS, Docker on Linux). The user never sees Docker.

If Docker setup fails, verification degrades to syntax+DNS only (no SMTP handshake).

## Crash recovery

All scrape jobs track per-tile progress in SQLite. On crash:
- Already-done tiles are skipped
- Stale tiles (stuck >5 min) reset to pending
- Restart the same job and it resumes

## Location formats

The agent can pass locations at any granularity:

| Input | Expands to |
|-------|-----------|
| `Austin, TX` | 1 tile |
| `Texas` | 1,025 tiles (Google Maps) or 995 autocomplete strings (LinkedIn) |
| `US` | ~10,000 tiles (Google Maps) or ~13,000 autocompletes (LinkedIn) |
| `Japan` | 1,339 tiles |
| `London, UK` | UK London only (disambiguated) |
| `London, Kentucky` | KY London only |
| `North Holland, Netherlands` | 40 tiles |
| `Seoul` | Seoul metro area |

Accented and non-accented forms work identically: `São Paulo` = `Sao Paulo`.

## MCP tools reference

| Tool | When to use |
|------|------------|
| `setup_status` | First thing. Check what's configured. |
| `configure` | Set an API key. Validates before saving. |
| `set_profile` | Onboarding. Scrape user's site for sender identity. |
| `get_profile` | Read the sender profile. |
| `scrape_google_maps` | Single Google Maps scrape (foreground). |
| `scrape_linkedin` | Single LinkedIn scrape (foreground). |
| `scrape_website` | Single website scrape (foreground). |
| `run_background_job` | Launch one scrape/enrich in background. |
| `run_batch_job` | Launch multiple jobs as a batch. |
| `get_job_status` | Check one job's progress. |
| `get_batch_status` | Check batch progress (all children). |
| `advance_batch` | Launch next pending children when slots open. |
| `enrich_leads` | Enrich unenriched leads (foreground). |
| `verify_emails` | Verify emails only (foreground). |
| `get_leads` | Query leads with filters. |
| `get_stats` | Lead counts by source and status. |
| `list_jobs` | List recent jobs. |
| `ensure_reacher` | Pre-start Reacher Docker. |
