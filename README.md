# suplex

Agent-driven lead scraping, enrichment, and email verification.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/apolloskim/suplex/main/install.sh | bash
suplex setup
```

Or via pip:

```bash
pip install suplex-ai
suplex setup
```

`suplex setup` installs the agent skill, registers the MCP server, sets up Playwright, and configures your API keys.

## What it does

- **Scrape** leads from Google Maps (free via Playwright or paid via Apify) and LinkedIn (Apify)
- **Enrich** with company website data, Google reviews, email verification, LinkedIn posts
- **Auto-enrich** after every scrape by default
- **Tile** across 241 countries — "Texas" becomes 1,025 city-level scrapes automatically
- **Resume** killed or failed jobs — picks up where it left off
- **Batch** multiple queries with auto-advancing watchdog (no polling needed)
- **Export** to CSV with source/status/enrichment filters
- **Budget-aware** — blocks Apify jobs when over limit
- **MCP server** with 20+ tools for AI agents (Claude Code, Codex, Copilot, Cursor)

## Quick start

```bash
suplex setup
suplex scrape maps "dentist" -l "Austin, TX" -s playwright -n 20
suplex scrape linkedin "dental hygienist" -l "Austin, TX" -n 20
suplex export -o dentists.csv
```

## Country-wide scraping

```bash
suplex regions Japan                                       # discover regions
suplex scrape maps "dentist" -l US --only WY,VT -n 20     # start small
suplex scrape maps "dentist" -l US --exclude WY,VT -n 20  # continue, skip done
suplex resume <job_id>                                     # pick up interrupted jobs
```

## For AI agents

Suplex works as an MCP server and ships an agent skill:

```bash
suplex serve
```

See [SKILL.md](skills/suplex/SKILL.md) for the full agent workflow.
