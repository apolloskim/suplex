---
name: suplex
description: >
  Scrape B2B leads from Google Maps and LinkedIn, enrich with website data,
  Google reviews, email verification, and LinkedIn posts. Use when the user
  asks to find leads, scrape contacts, build prospect lists, verify emails,
  or enrich business data. Handles geographic tiling across 241 countries.
  Check Apify budget before launching paid scrapes.
metadata:
  author: apolloskim
  version: "0.2.0"
argument-hint: <scrape|enrich|verify|batch>
---

# Suplex Lead Scraping Skill

## Before any scraping

1. Check setup: `suplex doctor`
2. Check Apify budget: `suplex budget` or `get_apify_budget` MCP tool
3. If budget is $0: use Playwright for Google Maps (free). LinkedIn requires Apify credits.

## Google Maps scraping

Default to **Playwright** (free, 500 places/day limit). Use **Apify** when user wants scale or speed.

```bash
suplex scrape maps "dentists" -l "Texas" -s playwright     # free
suplex scrape maps "dentists" -l "Texas" -s apify           # paid, check budget first
suplex scrape maps "dentists" -l "Austin, TX" -r 10         # 10 reviews per place
suplex scrape maps "dentists" -l "Austin, TX" -r 0          # skip reviews
```

Each place gets: name, rating, phone, address, website, category, hours, price level, Google Maps URL, and **reviews** (default 5 per place via RPC API — author, stars, date, text).

Geographic tiling is automatic: "Texas" → 1,025 tiles, "US" → ~10,000, "Japan" → 1,339.

Playwright daily limit: 500 places/day. For larger runs, spread across days or use Apify.

## LinkedIn scraping

Always Apify. Always full+email mode. Check budget first.

```bash
suplex scrape linkedin "dental hygienist" -l "Texas"
suplex scrape linkedin "" -l "US" -t "Dental Office Manager,Practice Manager"
suplex scrape linkedin "dentist" -l "Austin, TX" -t "Owner,Founder"
```

Each profile gets: name, email (pre-verified), title, headline, company, about, experience, education, skills, connections, followers, openToWork, hiring signals, company website.

Emails from LinkedIn are pre-verified by Apify — do NOT re-verify with Reacher.

LinkedIn tiling uses autocomplete strings from SQLite (17K cached). "Texas" → 995 unique LinkedIn geo entities. Disambiguation supported: "London, UK" vs "London, Kentucky".

## Budget awareness

**Always check budget before Apify jobs:**

```
get_apify_budget → {"used_usd": 85.00, "remaining_usd": 15.00, "plan_limit_usd": 100}
estimate_job_cost(type="linkedin", tile_count=995) → {"total_estimated_usd": 34.83}
```

If remaining < estimated cost: warn the user, suggest Playwright for Google Maps, or reduce scope.

Budget is enforced at runtime — if $0 remaining, Apify jobs are blocked and marked as `apify_budget_exhausted`.

## Batch scraping workflow

When user gives multiple queries/sources:

**Step 1:** Decompose into jobs.

"Scrape dental hygienists, dentist owners, and office managers across LinkedIn and Google Maps in Texas"

→ 6 jobs:
```json
run_batch_job({
  "jobs": [
    {"type": "scrape_maps", "query": "dental hygienist", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_maps", "query": "dentist", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_maps", "query": "dental office manager", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_linkedin", "query": "dental hygienist", "location": "Texas"},
    {"type": "scrape_linkedin", "query": "dentist owner", "location": "Texas", "titles": "Owner,Founder"},
    {"type": "scrape_linkedin", "query": "dental office manager", "location": "Texas", "titles": "Office Manager,Practice Manager"}
  ],
  "max_parallel": 3
})
```

**Step 2:** Monitor with `get_batch_status`. Call `advance_batch` periodically to launch next pending jobs.

**Step 3:** When all scrape jobs done, run enrichment:
```json
run_background_job({"type": "enrich"})
```

## Enrichment

Separate from scraping. Picks up all unenriched leads.

```bash
suplex enrich                     # all unenriched leads
suplex enrich --linkedin-posts 1  # default: 1 post per LinkedIn lead
suplex enrich --linkedin-posts 0  # skip posts (saves Apify credits)
```

Per lead:
1. **Site scrape** (free) — company website → business description, services, clean_text, emails, social links
2. **Email from site** — if found on website, use directly (even on catch-all domains)
3. **Email verification** — Reacher Docker SMTP handshake (free, auto-installed). Skipped for LinkedIn leads (pre-verified).
4. **Email guessing** — if no email: person names → permutations (first.last@domain), businesses → role addresses (info@, contact@). Blocked on catch-all domains.
5. **LinkedIn posts** — latest posts via Apify (~$0.002/lead). Conversation hooks.

## Email rules

- LinkedIn emails: pre-verified by Apify. Status stored as valid/catch_all/invalid.
- Site-extracted emails: real (business published them). Verify with Reacher.
- Guessed emails: only on non-catch-all domains. Verify before storing.
- Catch-all domains: site-extracted = keep. Guessed = reject.

## Crash recovery

All jobs track per-tile progress in SQLite. On crash:
- Done tiles skipped on restart
- Stale tiles (>5 min) reset to pending
- No data loss

## Location formats

| Input | Google Maps tiles | LinkedIn tiles |
|-------|------------------|---------------|
| `Austin, TX` | 1 | 1 |
| `Texas` | 1,025 | 995 |
| `US` | ~10,000 | ~13,000 |
| `Japan` | 1,339 | 1,682 |
| `London, UK` | UK only | UK only |
| `North Holland, Netherlands` | 40 | 40 |

Accents normalized: `São Paulo` = `Sao Paulo`.

## MCP tools

| Tool | When to use |
|------|------------|
| `setup_status` | First. Check keys + sender profile. |
| `configure` | Set an API key (validates before saving). |
| `get_apify_budget` | **Before any Apify job.** Check remaining credits. |
| `estimate_job_cost` | Estimate USD cost before running. |
| `set_profile` | Onboarding: scrape user's site for sender identity. |
| `get_profile` | Read sender profile. |
| `scrape_google_maps` | Single Maps scrape (foreground). |
| `scrape_linkedin` | Single LinkedIn scrape (foreground). |
| `scrape_website` | Single website scrape (foreground). |
| `run_background_job` | Launch one scrape/enrich in background. |
| `run_batch_job` | Launch multiple jobs as batch. |
| `get_job_status` | Check one job's progress. |
| `get_batch_status` | Check batch progress (all children). |
| `advance_batch` | Launch next pending children when slots open. |
| `enrich_leads` | Enrich unenriched leads (foreground). |
| `verify_emails` | Verify emails only (foreground). |
| `get_leads` | Query leads with filters. |
| `get_stats` | Lead counts by source and status. |
| `list_jobs` | List recent jobs. |
| `ensure_reacher` | Pre-start Reacher Docker. |
