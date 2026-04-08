---
name: suplex
description: >
  Scrape B2B leads from Google Maps and LinkedIn, enrich with website data,
  Google reviews, email verification, and LinkedIn posts. Export to CSV.
  Use when the user asks to find leads, scrape contacts, build prospect lists,
  verify emails, enrich business data, or export leads. Handles geographic
  tiling across 241 countries. Check Apify budget before paid scrapes.
metadata:
  author: apolloskim
  version: "0.3.0"
argument-hint: <scrape|enrich|export|batch>
---

# Suplex Lead Scraping Skill

## Before any scraping

1. `suplex doctor` — check services
2. `suplex budget` — check Apify credits
3. No budget? Use Playwright for Google Maps (free). LinkedIn requires Apify.

## Google Maps

Default: **Playwright** (free, 500 places/day). Reviews included (5 per place).

```bash
suplex scrape maps "dentists" -l "Texas" -s playwright     # free
suplex scrape maps "dentists" -l "Texas" -s apify           # paid
suplex scrape maps "dentists" -l "Austin, TX" -r 10         # 10 reviews
suplex scrape maps "dentists" -l "Austin, TX" -r 0          # skip reviews
```

Each lead gets: name, rating, review_count, phone, address, website, category, hours, price level, Google Maps URL, and reviews (author, stars, date, text).

Tiling is automatic. Playwright has human-like pacing (2-15s random delays). For >500 places, spread across days or use Apify.

## LinkedIn

Always Apify. Always full+email mode.

```bash
suplex scrape linkedin "dental hygienist" -l "Texas"
suplex scrape linkedin "" -l "US" -t "Office Manager,Practice Manager"
```

Each lead gets: name, email (pre-verified), title, headline, about, company, experience, education, skills, connections, followers, openToWork, hiring, company website.

**Do NOT re-verify LinkedIn emails** — Apify already checks them.

Tiling uses cached autocomplete strings. "Texas" → 995 LinkedIn geo entities.

## Budget check

**Always before Apify jobs:**

```
suplex budget
→ Used: $85.00 / $200.00, Remaining: $115.00
```

Or via MCP: `get_apify_budget` then `estimate_job_cost`.

Budget enforced at runtime — over-budget Apify jobs are blocked.

## Batch workflow

Decompose complex requests into jobs:

```json
run_batch_job({
  "jobs": [
    {"type": "scrape_maps", "query": "dental hygienist", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_maps", "query": "pediatric dentist", "location": "Texas", "scraper": "playwright"},
    {"type": "scrape_linkedin", "query": "dental hygienist", "location": "Texas"},
    {"type": "scrape_linkedin", "query": "dentist owner", "location": "Texas", "titles": "Owner,Founder"}
  ],
  "max_parallel": 3
})
```

Monitor: `get_batch_status`. Advance: `advance_batch` (launches next pending when slots open).

When done, enrich: `run_background_job({"type": "enrich"})`.

## Enrichment

Runs on all unenriched leads. Separate from scraping — check `get_stats` for unenriched count.

```bash
suplex enrich                     # all unenriched leads
suplex enrich --linkedin-posts 1  # fetch 1 LinkedIn post per lead (default)
suplex enrich --linkedin-posts 0  # skip posts (saves credits)
```

Per lead:
1. **Site scrape** (free) — business data, clean_text, emails, social links
2. **Email from site** — use directly, even on catch-all domains
3. **Email verification** — Reacher Docker (free, auto-installed). Skipped for LinkedIn (pre-verified).
4. **Email guessing** — businesses: info@, contact@, hello@. Persons: first.last@. Blocked on catch-all domains. Only verified emails stored.
5. **LinkedIn posts** — conversation hooks (~$0.002/lead)

## Using enriched data for outreach

The agent reads `raw_data` and `enrichment_data` directly — no separate LLM call needed.

**Google Maps leads — use these signals:**
- `reviews[]` — reference specific reviews ("I saw your 5-star review about...")
- `rating` + `review_count` — "Your 4.9 rating across 580 reviews is impressive"
- `clean_text` from site scrape — understand what they do, their services
- Negative reviews — pain points ("I noticed some patients mentioned wait times...")

**LinkedIn leads — use these signals:**
- `headline` + `about` — what they care about
- `experience[]` — career trajectory, recent job change
- `skills[]` — technical expertise
- `openToWork` / `hiring` — timing signals
- `connectionsCount` / `followerCount` — influence level
- `linkedin_posts[]` from enrichment — "I saw your post about..."

## Export

```bash
suplex export                              # all leads → leads.csv
suplex export -s google_maps -o gmaps.csv  # Google Maps only
suplex export -s linkedin -o linkedin.csv  # LinkedIn only
suplex export -v valid -o verified.csv     # verified emails only
suplex export -e -o enriched.csv           # enriched only
```

## Email rules

- LinkedIn emails: pre-verified by Apify. Do not re-verify.
- Site-extracted emails: real. Verify with Reacher.
- Guessed emails: verified during guessing. Stored only if valid.
- Catch-all domains: site-extracted = keep. Guessed = reject.

## Resume and crash recovery

Every tile is tracked in SQLite (pending → processing → done/failed). On crash, daily limit, or budget exhaustion:
- Done tiles are skipped
- Failed/stale tiles reset to pending
- Resume with the same job_id to continue

```bash
suplex jobs                    # find the incomplete job
suplex resume <job_id>         # picks up where it left off
```

Or via MCP: `resume_job(job_id)` — resets failed tasks, relaunches with same command.

**Daily Playwright workflow:** run 500 tiles today, hit the limit, resume tomorrow:
```
Day 1: suplex run scrape-maps "dentist" -l Texas -s playwright → job_id abc123 (500 done, 525 remaining)
Day 2: suplex resume abc123 → picks up at tile 501
Day 3: suplex resume abc123 → finishes remaining 25
```

## Location formats

| Input | Google Maps | LinkedIn |
|-------|-----------|---------|
| `Austin, TX` | 1 tile | 1 tile |
| `Texas` | 1,025 | 995 |
| `US` | ~10,000 | ~13,000 |
| `Japan` | 1,339 | 1,682 |
| `London, UK` | UK only | UK only |

Accents normalized: `São Paulo` = `Sao Paulo`. Disambiguation: `London, UK` vs `London, Kentucky`.

## MCP tools

| Tool | When |
|------|------|
| `setup_status` | First. Check config + profile. |
| `configure` | Set API key (validates). |
| `get_apify_budget` | **Before Apify jobs.** |
| `estimate_job_cost` | Estimate cost before running. |
| `set_profile` | Onboarding: user's site → sender identity. |
| `get_profile` | Read sender profile. |
| `scrape_google_maps` | Maps scrape (foreground). |
| `scrape_linkedin` | LinkedIn scrape (foreground). |
| `scrape_website` | Website scrape (foreground). |
| `run_background_job` | Launch scrape/enrich in background. |
| `run_batch_job` | Launch multiple jobs as batch. |
| `get_job_status` | Check one job. |
| `get_batch_status` | Check batch (all children). |
| `advance_batch` | Launch next pending children. |
| `enrich_leads` | Enrich (foreground). |
| `verify_emails` | Verify only (foreground). |
| `get_leads` | Query with filters. |
| `get_stats` | Counts by source + status. |
| `resume_job` | Resume stopped/failed job. |
| `list_jobs` | Recent jobs. |
| `ensure_reacher` | Pre-start Docker. |
