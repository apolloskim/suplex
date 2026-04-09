---
name: suplex
description: >
  Scrape and enrich B2B leads from Google Maps and LinkedIn, then export CSVs.
  Use when the user asks to find prospects, scrape contacts, build lead lists,
  run background jobs, resume interrupted jobs, verify or enrich business data,
  or export leads. Handles region-aware tiling across 241 countries. Check
  Apify budget before paid scrapes and always pass -n for max results.
metadata:
  author: apolloskim
  version: "0.5.0"
argument-hint: <scrape|run|resume|enrich|export|batch>
---

# Suplex Lead Scraping Skill

## Operating rules

1. `suplex doctor` before scraping
2. `suplex budget` before Apify jobs
3. Always pass `-n` for max results. Never exhaust without user confirmation.
4. Do not re-verify LinkedIn emails. Apify already pre-verifies them.
5. Use `suplex regions` before `--only` / `--exclude` when the local region name may differ from English.

## Google Maps

Two scrapers. Agent chooses deliberately; there is no auto mode.

**Playwright** (default, free, 1000 places/day):
```bash
suplex scrape maps "dentists" -l "Texas" -n 20
suplex scrape maps "dentists" -l "US" --only WY,VT -n 20
```

**Apify** (paid, 10-30x more results, state polygons for US):
```bash
suplex scrape maps "dentists" -l "Texas" -s apify -n 50
suplex scrape maps "dentists" -l "US" --exclude WY,VT -s apify -n 100
```

Scraping auto-enriches by default. Add `--no-enrich` to skip.

**Region filters:** `--only` / `--exclude` target or skip states, regions, or country codes. Check `suplex jobs` for completed regions and exclude them to avoid wasting time and credits.

Each lead gets: name, rating, review_count, phone, address, website, category, hours, price level, Google Maps URL, reviews (author, stars, date, text).

Playwright: human-like pacing (2-15s delays), coordinate-based viewport search.
Apify: polygon-based for US states (`customGeolocation`), text-based for non-US.

## LinkedIn

Always Apify. Always `full_with_email` mode. Always pass `-n`.

`-s` is **not** a LinkedIn flag. It only applies to Google Maps resumes. If someone tries `suplex resume <linkedin_job_id> -s apify`, Suplex ignores it and keeps the original LinkedIn filters.

Foreground CLI, background CLI, batch jobs, and MCP all support the same LinkedIn filter family. Use that parity instead of inventing one-off workflows.

### Query construction

The query supports LinkedIn boolean operators:
- `AND` — both terms required: `"dentist AND owner"`
- `OR` — either term: `"dentist OR dental hygienist"`
- `NOT` — exclude: `"dentist NOT assistant"`
- `"quotes"` — exact phrase: `"dental practice owner"`
- `(parens)` — grouping: `"dentist AND (owner OR founder) NOT assistant"`

Operators must be uppercase. Raw text like `dentist owner` is fuzzy and looser than it looks.

### Filters

Use filters with the query. The query finds candidates; filters constrain them.

**Most useful filters:**

| Flag | Actor param | When to use |
|------|-------------|-------------|
| `--seniority 320` | `seniorityLevelIds` | Owners and partners (`320=Owner/Partner`, `310=CXO`) |
| `--functions 11` | `functionIds` | Restrict by function (`11=Healthcare`, `8=Engineering`) |
| `--industries 901` | `industryIds` | Restrict by industry ([full list](https://github.com/HarvestAPI/linkedin-industry-codes-v2)) |
| `--headcount A,B` | `companyHeadcount` | Small practices (`A=Self-employed`, `B=1-10`) |
| `-t "Dentist"` | `currentJobTitles` | Exact current title match; must be profession-specific |

**Seniority levels:**
`100`=In Training, `110`=Entry, `120`=Senior, `130`=Strategic, `200`=Entry Manager, `210`=Experienced Manager, `220`=Director, `300`=VP, `310`=CXO, `320`=Owner/Partner

**Function IDs:**
`1`=Accounting, `2`=Administrative, `3`=Arts/Design, `4`=BizDev, `5`=Community/Social, `6`=Consulting, `7`=Education, `8`=Engineering, `9`=Entrepreneurship, `10`=Finance, `11`=Healthcare Services, `12`=HR, `13`=IT, `14`=Legal, `15`=Marketing, `16`=Media, `17`=Military, `18`=Operations, `19`=Product, `20`=Program/Project Mgmt, `21`=Purchasing, `22`=QA, `23`=Real Estate, `24`=Research, `25`=Sales, `26`=Customer Success

**Company headcount:** `A`=Self-employed, `B`=1-10, `C`=11-50, `D`=51-200, `E`=201-500, `F`=501-1K, `G`=1K-5K, `H`=5K-10K, `I`=10K+

**Other supported filters:**

| Flag | Actor param | Values |
|------|-------------|--------|
| `--past-titles` | `pastJobTitles` | Exact match on past titles |
| `-c, --companies` | `currentCompanies` | LinkedIn company URLs |
| `--past-companies` | `pastCompanies` | LinkedIn company URLs |
| `--schools` | `schools` | School names such as `"Stanford University"` |
| `--years-exp` | `yearsOfExperienceIds` | `1=<1yr`, `2=1-2yr`, `3=3-5yr`, `4=6-10yr`, `5=10+yr` |
| `--years-at-company` | `yearsAtCurrentCompanyIds` | Same 1-5 scale |
| `--company-hq` | `companyHeadquarterLocations` | HQ location text |
| `--profile-languages` | `profileLanguages` | English, Spanish, French, German, etc. |
| `--recently-changed-jobs` | `recentlyChangedJobs` | Boolean timing signal |

### Examples

```bash
# Dentist practice owners: boolean query + seniority + function + headcount
suplex scrape linkedin "dentist" -l "Texas" --seniority 320 --functions 11 --headcount A,B -n 50

# Same search in the background
suplex run scrape-linkedin "dentist" -l "Texas" --seniority 320 --functions 11 --headcount A,B -n 50

# Boolean query with a specific title
suplex scrape linkedin "dentist AND owner" -l "Texas" -t "Dental Practice Owner,Owner Dentist" -n 50

# Dental hygienists
suplex scrape linkedin "dental hygienist" -l "Texas" --functions 11 -n 30

# CEOs at small tech companies
suplex scrape linkedin "CEO" -l "California" --seniority 310 --functions 13 --headcount B,C -n 50

# Recently changed jobs
suplex scrape linkedin "dentist" -l "US" --only TX --recently-changed-jobs -n 20

# State or region filters
suplex scrape linkedin "dentist" -l "US" --only CA,TX -n 20
suplex scrape linkedin "dentist" -l "US" --exclude WY -n 20

# BAD: generic title without structural filters
# suplex scrape linkedin "dentist" -t "Owner" -n 20
```

### Title rules

`-t` is an exact current-title match on LinkedIn.

1. Never use generic titles alone: `Owner`, `Founder`, `Manager`
2. If a generic title is necessary, combine it with seniority, function, or industry filters
3. Prefer boolean query + structural filters over `-t` for most searches
4. `-t` works best for specific titles such as `Dental Hygienist`, `RDH`, `Pediatric Dentist`

Each lead gets: name, email (pre-verified), title, headline, about, company, experience, education, skills, connections, followers, `openToWork`, `hiring`, company website.

## Background jobs

Use background jobs when the scrape may outlive the terminal session:

```bash
suplex run scrape-maps "dentists" -l "Texas" -s playwright -n 50
suplex run scrape-linkedin "dentist" -l "Texas" --seniority 320 --functions 11 --headcount A,B -n 50
suplex run enrich
```

Direct background jobs round-trip all CLI flags through the subprocess launch. For LinkedIn, that includes the full filter set above. For Maps, that includes `--only`, `--exclude`, `-n`, and `-s`.

## Batch jobs

Use batch jobs for multi-query or multi-source campaigns. Watchdog auto-advances; no polling loop required.

```json
run_batch_job({
  "jobs": [
    {
      "type": "scrape_maps",
      "query": "dentists",
      "location": "Texas",
      "scraper": "playwright",
      "max_results": 50
    },
    {
      "type": "scrape_linkedin",
      "query": "dentist",
      "location": "Texas",
      "seniority": "320",
      "functions": "11",
      "headcount": "A,B",
      "max_results": 50
    },
    {
      "type": "scrape_linkedin",
      "query": "dental hygienist",
      "location": "Texas",
      "functions": "11",
      "recently_changed_jobs": true,
      "max_results": 30
    },
    {
      "type": "enrich",
      "limit": 100,
      "linkedin_posts": 0
    }
  ],
  "max_parallel": 3
})
```

Returns `batch_id`. Use `get_batch_status` to inspect the whole batch.

Batch child LinkedIn scrapes preserve all filters on launch and on resume.

## Enrichment

Auto-runs after scraping by default. Also available standalone:

```bash
suplex enrich
suplex enrich --linkedin-posts 0
```

Per lead:
1. Site scrape (free): business data, `clean_text`, emails, social links
2. Email from site: use directly, even on catch-all domains
3. Email verification: Reacher Docker (free, auto-installed). Skipped for LinkedIn.
4. Email guessing: businesses use `info@` / `contact@`; people use `first.last@`; blocked on catch-all
5. LinkedIn posts: only for leads with email

## Resume

Jobs track per-tile progress. Kill -> resume picks up where it left off.

```bash
suplex jobs
suplex resume <job_id>
suplex resume <maps_job_id> -s apify
```

Resume rules:
- Direct jobs and batch child jobs preserve their original filters
- `-s apify` is only for Google Maps jobs
- LinkedIn resumes ignore `-s` with a warning because LinkedIn always uses Apify
- If a Maps job is resumed with `-s apify`, that override is persisted for later resumes too

Daily workflow:
```text
Day 1: suplex run scrape-maps "dentist" -l Texas -n 3
Day 2: suplex resume <job_id>
  OR:  suplex resume <maps_job_id> -s apify
```

## Budget

Always check before Apify jobs:

```text
suplex budget -> Used: $85.00 / $200.00, Remaining: $115.00
```

Budget is enforced at runtime. Over-budget Apify jobs are blocked.

## Email rules

- LinkedIn: pre-verified by Apify. Do not re-verify.
- Site-extracted: verify with Reacher.
- Guessed: verify during guessing. Store only if valid.
- Catch-all: keep site emails, reject guesses.

## Discover locations

Before using `--only` / `--exclude`, discover local region names:

```bash
suplex regions Japan
suplex regions --search Rotterdam
suplex regions --search Tokyo
suplex regions --search "North Holland"
```

## Incremental country-wide scraping

When scraping a whole country:

1. `suplex jobs` to see completed jobs and locations
2. `suplex job <id>` to inspect tile completion
3. Exclude completed regions on the next run

```bash
suplex scrape maps "dentist" -l US --only WY,VT -n 20
suplex scrape maps "dentist" -l US --only MT,ND,SD -n 20
suplex scrape maps "dentist" -l US --exclude WY,VT,MT,ND,SD -n 20
```

## Using enriched data for outreach

**Google Maps leads:**
- `reviews[]` for personalized outreach
- `rating` and `review_count` as health signals
- `clean_text` for services and positioning

**LinkedIn leads:**
- `headline` and `about` for positioning
- `experience[]` for career trajectory and recency
- `openToWork` / `hiring` / `recently_changed_jobs` as timing signals
- `linkedin_posts[]` for direct references in outreach

## Export

```bash
suplex export
suplex export -s google_maps -o gmaps.csv
suplex export -s linkedin -v valid -o li.csv
suplex export -e -o enriched.csv
```

## Location formats

| Input | Google Maps | LinkedIn |
|-------|-------------|----------|
| `Austin, TX` | 1 tile | 1 tile |
| `Texas` | 1,025 | 995 |
| `US` | ~10,000 | ~13,000 |
| `Japan` | 1,339 | 1,682 |
| `London, UK` | UK only | UK only |

Accents: `Sao Paulo` and `São Paulo` both work. Disambiguation: `London, UK` vs `London, Kentucky`.
Filters are accent-insensitive: `--only Tokyo` matches `Tōkyō`; `--only Noord-Holland` works for Dutch regions.

## MCP tools

| Tool | When |
|------|------|
| `setup_status` | First. Check config and sender profile. |
| `configure` | Set API keys. |
| `get_apify_budget` | Before Apify jobs. |
| `estimate_job_cost` | Estimate cost before running. |
| `set_profile` | Onboarding: website -> sender identity. |
| `get_profile` | Read sender profile. |
| `scrape_google_maps` | Maps scrape in foreground. |
| `scrape_linkedin` | LinkedIn scrape in foreground with full filter support. |
| `scrape_website` | Website scrape in foreground. |
| `run_background_job` | Launch one scrape or enrich job; LinkedIn supports the same filters as CLI. |
| `run_batch_job` | Launch multiple jobs; batch LinkedIn filters and enrich knobs are supported. |
| `get_job_status` | Check one job. |
| `get_batch_status` | Check a batch and its children. |
| `resume_job` | Resume stopped or failed jobs; Maps may switch scrapers, LinkedIn keeps its original filters. |
| `enrich_leads` | Enrich in foreground. |
| `verify_emails` | Verify only in foreground. |
| `get_leads` | Query stored leads with filters. |
| `get_stats` | Counts by source and verification status. |
| `list_jobs` | Recent jobs. |
| `ensure_reacher` | Pre-start Docker for Reacher. |
| `get_outreach_playbook` | Read playbook; if none exists, walk user through creating one. |
| `update_outreach_playbook` | Save updated playbook content. |
| `list_mailboxes` | List sender mailboxes with today's send count. |
| `add_mailbox` | Register a new Gmail mailbox for outreach. |
| `create_campaign` | Create an outreach campaign (filters, cadence, mailbox, address). |
| `preview_outreach` | Get eligible leads + playbook for drafting. |
| `save_outreach_drafts` | Save agent-generated email drafts. |
| `approve_outreach_drafts` | Approve drafts for sending. |
| `send_outreach` | Send approved emails via gws Gmail. |
| `check_outreach_replies` | Poll Gmail for replies; auto-detect opt-outs. |
| `get_due_followups` | Get leads due for follow-up. |
| `get_outreach_stats` | Campaign stats: drafted, sent, replied, etc. |

## Outreach

Outreach uses the Google Workspace CLI (`gws`) to send emails from the user's own Gmail.

### Setup

1. Install gws: `npm install -g @anthropic/gws` or `brew install gws`
2. Register a mailbox: `suplex mailbox add user@gmail.com`
3. Authenticate: run the auth command printed by step 2
4. Verify: `suplex mailbox verify <mailbox_id>`
5. Create playbook: `suplex playbook init` (or agent walks user through it)

### Agent workflow

Before drafting emails, ALWAYS:
1. Check if a playbook exists (`get_outreach_playbook`)
2. If no playbook, interview the user: tone, angle, CTA, what to avoid — then save it
3. If playbook exists, show the **summary** (not full content) and ask if they want to adjust
4. Never send without user reviewing drafts first

The playbook is the user's outreach personality. Update it when they give feedback on drafts.

### Drafting flow

1. `preview_outreach` — get eligible leads + playbook + sender profile
2. Agent generates personalized emails using lead enrichment data (reviews, posts, headline, about)
3. `save_outreach_drafts` — store drafts
4. Show drafts to user for review
5. `approve_outreach_drafts` — user approves
6. `send_outreach` — send via gws with rate limits

### Compliance (CAN-SPAM)

Every outreach email MUST include:
- A conversational opt-out line (not "unsubscribe" — use natural language like "Not relevant? Just let me know.")
- The campaign's physical postal address
- These are non-negotiable. The system will not send without them.

Opt-out detection is automatic: when a reply contains "stop", "not interested", "remove me", etc., the lead is added to the suppression list. The agent reading the reply IS the classifier.

### Follow-ups

Default cadence: initial send, +3 days, +7 days. Configurable per campaign.
- Follow-ups are sent as replies in the same Gmail thread
- Agent generates fresh copy for each step (different angle, shorter)
- Stop automatically on: reply, bounce, opt-out
- `get_due_followups` → agent drafts → user approves → `send_outreach`

### Rate limits

- Default: 10 emails/day per mailbox (configurable, max recommended 20)
- Sends today is derived from the outreach table, not a stored counter
- Multiple mailboxes can be registered for rotation
