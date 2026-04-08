---
name: suplex
description: >
  Scrape B2B leads from Google Maps and LinkedIn, enrich with website data,
  Google reviews, email verification, and LinkedIn posts. Export to CSV.
  Use when the user asks to find leads, scrape contacts, build prospect lists,
  verify emails, enrich business data, or export leads. Handles geographic
  tiling across 241 countries. Check Apify budget before paid scrapes.
  Always pass -n for max results.
metadata:
  author: apolloskim
  version: "0.4.0"
argument-hint: <scrape|enrich|export|batch>
---

# Suplex Lead Scraping Skill

## Before any scraping

1. `suplex doctor` — check services
2. `suplex budget` — check Apify credits
3. **Always pass `-n` for max results.** Never exhaust without user confirmation.

## Google Maps

Default: **Playwright** (free, 500 places/day). Reviews included (5 per place).

```bash
suplex scrape maps "dentists" -l "Texas" -s playwright -n 20
suplex scrape maps "dentists" -l "Austin, TX" -s apify -n 50
suplex scrape maps "dentists" -l "Austin, TX" -r 0 -n 20     # skip reviews
```

Scraping auto-enriches by default. Add `--no-enrich` to skip.

Each lead gets: name, rating, review_count, phone, address, website, category, hours, price level, Google Maps URL, reviews (author, stars, date, text).

Tiling is automatic. Playwright has human-like pacing (2-15s random delays).

## LinkedIn

Always Apify. Always full+email mode. **Always pass `-n`.**

```bash
suplex scrape linkedin "dental hygienist" -l "Texas" -n 20
suplex scrape linkedin "" -l "US" -t "Office Manager" -n 50
```

Each lead gets: name, email (pre-verified), title, headline, about, company, experience, education, skills, connections, followers, openToWork, hiring, company website.

**Do NOT re-verify LinkedIn emails.** Apify already checks them.

## Budget

**Always check before Apify jobs:**

```
suplex budget → Used: $85.00 / $200.00, Remaining: $115.00
```

Budget enforced at runtime — over-budget Apify jobs blocked.

## Batch jobs

For multiple queries/sources, use `run_batch_job`. Watchdog auto-advances — no polling needed.

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

Returns `batch_id`. Check with `get_batch_status`. Jobs auto-advance when slots open.

## Enrichment

Auto-runs after scraping by default. Also available standalone:

```bash
suplex enrich                     # all unenriched leads
suplex enrich --linkedin-posts 0  # skip posts (saves credits)
```

Per lead:
1. **Site scrape** (free) — business data, clean_text, emails, social links
2. **Email from site** — use directly, even on catch-all domains
3. **Email verification** — Reacher Docker (free, auto-installed). Skipped for LinkedIn.
4. **Email guessing** — businesses: info@, contact@. Persons: first.last@. Blocked on catch-all. Only verified emails stored.
5. **LinkedIn posts** — only for leads WITH email (~$0.002/lead)

## Resume

Jobs track per-tile progress. Kill → resume picks up where it left off.

```bash
suplex jobs              # find incomplete job
suplex resume <job_id>   # done tiles skipped, failed tiles retried
```

Daily Playwright workflow:
```
Day 1: suplex run scrape-maps "dentist" -l Texas -s playwright -n 3 → hits 500 limit
Day 2: suplex resume <job_id> → continues from where it stopped
```

## Using enriched data for outreach

**Google Maps leads:**
- `reviews[]` — "I saw your 5-star review about..."
- `rating` + `review_count` — business health signal
- `clean_text` — what they do, their services
- Negative reviews — pain points

**LinkedIn leads:**
- `headline` + `about` — what they care about
- `experience[]` — career trajectory, recent job change
- `openToWork` / `hiring` — timing signals
- `linkedin_posts[]` — "I saw your post about..."

## Export

```bash
suplex export                              # all → leads.csv
suplex export -s google_maps -o gmaps.csv  # by source
suplex export -s linkedin -v valid -o li.csv  # verified LinkedIn
suplex export -e -o enriched.csv           # enriched only
```

## Email rules

- LinkedIn: pre-verified by Apify. Do not re-verify.
- Site-extracted: real. Verify with Reacher.
- Guessed: verified during guessing. Stored only if valid.
- Catch-all: site emails = keep. Guesses = reject.

## Location formats

| Input | Google Maps | LinkedIn |
|-------|-----------|---------|
| `Austin, TX` | 1 tile | 1 tile |
| `Texas` | 1,025 | 995 |
| `US` | ~10,000 | ~13,000 |
| `Japan` | 1,339 | 1,682 |
| `London, UK` | UK only | UK only |

Accents: `São Paulo` = `Sao Paulo`. Disambiguation: `London, UK` vs `London, Kentucky`.

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
| `run_background_job` | Launch one scrape/enrich in background. |
| `run_batch_job` | Launch multiple jobs. Watchdog auto-advances. |
| `get_job_status` | Check one job. |
| `get_batch_status` | Check batch (all children). |
| `resume_job` | Resume stopped/failed job. |
| `enrich_leads` | Enrich (foreground). |
| `verify_emails` | Verify only (foreground). |
| `get_leads` | Query with filters. |
| `get_stats` | Counts by source + status. |
| `list_jobs` | Recent jobs. |
| `ensure_reacher` | Pre-start Docker. |
