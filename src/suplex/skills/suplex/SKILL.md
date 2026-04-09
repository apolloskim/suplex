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

Two scrapers. Agent decides — no auto mode.

**Playwright** (default, free, 500 places/day):
```bash
suplex scrape maps "dentists" -l "Texas" -n 20
suplex scrape maps "dentists" -l "US" --only WY,VT -n 20
```

**Apify** (paid, 10-30x more results, uses state polygons for US):
```bash
suplex scrape maps "dentists" -l "Texas" -s apify -n 50
suplex scrape maps "dentists" -l "US" --exclude WY,VT -s apify -n 100
```

Playwright → Apify switch on resume:
```bash
suplex resume <job_id> -s apify    # hit 500 limit, continue with Apify
```

Scraping auto-enriches by default. Add `--no-enrich` to skip.

**State filters:** `--only` / `--exclude` to target or skip regions. Check `suplex jobs` for completed regions — exclude them to avoid wasting time and credits.

Each lead gets: name, rating, review_count, phone, address, website, category, hours, price level, Google Maps URL, reviews (author, stars, date, text).

Playwright: human-like pacing (2-15s delays), coordinate-based viewport search.
Apify: polygon-based for US states (customGeolocation), text-based for non-US.

## LinkedIn

Always Apify. Always full+email mode. **Always pass `-n`.**

### Query construction (searchQuery)

The query supports **LinkedIn boolean operators** — use them for precision:
- `AND` — both terms required: `"dentist AND owner"`
- `OR` — either term: `"dentist OR dental hygienist"`
- `NOT` — exclude: `"dentist NOT assistant"`
- `"quotes"` — exact phrase: `"dental practice owner"`
- `(parens)` — grouping: `"dentist AND (owner OR founder) NOT assistant"`

Operators must be **UPPERCASE**. Raw text like `dentist owner` is fuzzy and will match loosely.

### Filters (narrow results precisely)

Use filters in combination with the query. The query finds candidates; filters constrain them.

**Most useful filters — use proactively:**

| Flag | Actor param | When to use |
|------|------------|-------------|
| `--seniority 320` | seniorityLevelIds | Find owners/partners (320=Owner/Partner, 310=CXO) |
| `--functions 11` | functionIds | Restrict to function (11=Healthcare, 8=Engineering) |
| `--industries 901` | industryIds | Restrict to industry ([full list](https://github.com/HarvestAPI/linkedin-industry-codes-v2)) |
| `--headcount A,B` | companyHeadcount | Small practices (A=Self-employed, B=1-10) |
| `-t "Dentist"` | currentJobTitles | Exact title match — must be specific, not generic |

**Seniority levels:**
`100`=In Training, `110`=Entry, `120`=Senior, `130`=Strategic, `200`=Entry Manager, `210`=Experienced Manager, `220`=Director, `300`=VP, `310`=CXO, `320`=Owner/Partner

**Function IDs:**
`1`=Accounting, `2`=Administrative, `3`=Arts/Design, `4`=BizDev, `5`=Community/Social, `6`=Consulting, `7`=Education, `8`=Engineering, `9`=Entrepreneurship, `10`=Finance, `11`=Healthcare Services, `12`=HR, `13`=IT, `14`=Legal, `15`=Marketing, `16`=Media, `17`=Military, `18`=Operations, `19`=Product, `20`=Program/Project Mgmt, `21`=Purchasing, `22`=QA, `23`=Real Estate, `24`=Research, `25`=Sales, `26`=Customer Success

**Company headcount:** `A`=Self-employed, `B`=1-10, `C`=11-50, `D`=51-200, `E`=201-500, `F`=501-1K, `G`=1K-5K, `H`=5K-10K, `I`=10K+

**Other filters (use when user asks):**

| Flag | Actor param | Values |
|------|------------|--------|
| `--past-titles` | pastJobTitles | Exact match on past titles |
| `-c, --companies` | currentCompanies | LinkedIn company URLs |
| `--past-companies` | pastCompanies | LinkedIn company URLs |
| `--schools` | schools | School names (e.g. "Stanford University") |
| `--years-exp` | yearsOfExperienceIds | 1=<1yr, 2=1-2yr, 3=3-5yr, 4=6-10yr, 5=10+yr |
| `--years-at-company` | yearsAtCurrentCompanyIds | Same 1-5 scale |
| `--company-hq` | companyHeadquarterLocations | HQ location text |
| `--profile-languages` | profileLanguages | English, Spanish, French, German, etc. |
| `--recently-changed-jobs` | recentlyChangedJobs | Boolean — timing signal for outreach |

### Examples

```bash
# Dentist practice owners — use boolean query + seniority + function + headcount
suplex scrape linkedin "dentist" -l "Texas" --seniority 320 --functions 11 --headcount A,B -n 50

# Alternative: boolean query with specific title
suplex scrape linkedin "dentist AND owner" -l "Texas" -t "Dental Practice Owner,Owner Dentist" -n 50

# Dental hygienists — query is specific enough, add function for precision
suplex scrape linkedin "dental hygienist" -l "Texas" --functions 11 -n 30

# CEOs at small tech companies
suplex scrape linkedin "CEO" -l "California" --seniority 310 --functions 13 --headcount B,C -n 50

# Recently changed jobs (timing signal for outreach)
suplex scrape linkedin "dentist" -l "US" --only TX --recently-changed-jobs -n 20

# State filters
suplex scrape linkedin "dentist" -l "US" --only CA,TX -n 20
suplex scrape linkedin "dentist" -l "US" --exclude WY -n 20

# BAD: generic title without other filters — returns garbage
# suplex scrape linkedin "dentist" -t "Owner" -n 20  ← Owner matches EVERY owner
```

### Title filter rules

`-t` is an **exact title match** on LinkedIn. Rules:
1. **Never use generic titles alone** ("Owner", "Founder", "Manager") — they match every profession
2. **Combine with seniority/function/industry** if you must use generic titles
3. **Prefer boolean query + structural filters** over `-t` for most searches
4. `-t` is best for very specific titles: "Dental Hygienist", "RDH", "Pediatric Dentist"

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

Jobs track per-tile progress. Kill → resume picks up where it left off. Can switch scraper on resume.

```bash
suplex jobs                           # find incomplete job
suplex resume <job_id>                # same scraper, continue
suplex resume <job_id> -s apify       # switch to Apify for remaining tiles
```

Daily workflow:
```
Day 1: suplex run scrape-maps "dentist" -l Texas -n 3  → hits 500 Playwright limit
Day 2: suplex resume <job_id>                          → continue with Playwright
  OR:  suplex resume <job_id> -s apify                 → finish faster with Apify
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

## Discover locations

Before using `--only`/`--exclude`, discover local region names:

```bash
suplex regions Japan                    # list all regions with city counts
suplex regions --search Rotterdam       # find: Zuid-Holland, NL + LinkedIn autocompletes
suplex regions --search Tokyo           # find: Tōkyō, JP
suplex regions --search "North Holland" # find local name to use in --only
```

## Incremental country-wide scraping

When scraping a whole country, check what's already done and exclude it:

1. `suplex jobs` → see completed jobs with locations
2. `suplex job <id>` → check tile completion (e.g., 57/57 = fully done)
3. Next run: `--exclude` the done regions

```bash
# Day 1: scrape Wyoming and Vermont
suplex scrape maps "dentist" -l US --only WY,VT -n 20
# Day 2: check jobs, WY+VT done → scrape next states
suplex scrape maps "dentist" -l US --only MT,ND,SD -n 20
# Or exclude what's done
suplex scrape maps "dentist" -l US --exclude WY,VT,MT,ND,SD -n 20
```

## Location formats

| Input | Google Maps | LinkedIn |
|-------|-----------|---------|
| `Austin, TX` | 1 tile | 1 tile |
| `Texas` | 1,025 | 995 |
| `US` | ~10,000 | ~13,000 |
| `Japan` | 1,339 | 1,682 |
| `London, UK` | UK only | UK only |

Accents: `São Paulo` = `Sao Paulo`. Disambiguation: `London, UK` vs `London, Kentucky`.
Filters: `--only Tokyo` matches `Tōkyō`. `--only Noord-Holland` for Dutch regions.

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
