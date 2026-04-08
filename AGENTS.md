# Suplex — Agent-Driven Lead Scraping & Verification

You are interacting with **Suplex**, a CLI + MCP tool for autonomous lead generation.

## What it does

- **Scrapes** leads from Google Maps (via Apify), LinkedIn (via Apify), and websites (via Playwright)
- **Verifies** emails via SMTP handshake (Reacher Docker, free) + catch-all detection (MillionVerifier)
- **Plans** scrape strategies using AI (Claude) — you describe the business, AI decides what to scrape
- **Schedules** recurring autonomous scrape runs via cron

## Quick start

```bash
# Install
cd /Users/soyeonjo/apollos/suplex
uv sync
uv run playwright install chromium

# Setup (interactive — configures API keys + Reacher Docker)
uv run suplex setup

# Check health
uv run suplex doctor
```

## CLI commands

```bash
# AI-plan what to scrape, then execute it
uv run suplex plan "B2B SaaS for restaurants" --execute

# Scrape specific sources
uv run suplex scrape maps "pizza shops" -l "Austin TX" -n 50
uv run suplex scrape linkedin "CTO SaaS" -l "San Francisco" -n 25
uv run suplex scrape website https://example.com

# Verify emails in the database
uv run suplex verify -n 100

# View results
uv run suplex leads --source google_maps --status valid
uv run suplex stats

# Schedule recurring runs
uv run suplex schedule "B2B SaaS for restaurants" -c "0 9 * * 1"

# Start MCP server (for agent tool discovery)
uv run suplex serve
```

## MCP tools (when running as MCP server)

| Tool | Description |
|------|-------------|
| `setup_status` | Check which API keys are configured. **Call this first.** |
| `configure` | Set and validate an API key |
| `ensure_reacher` | Auto-start Reacher Docker for email verification |
| `plan_scrape` | AI generates a scrape plan from business domain + preferences |
| `run_scrape_plan` | Plan + execute in one step |
| `scrape_google_maps` | Scrape Google Maps via Apify |
| `scrape_linkedin` | Scrape LinkedIn via Apify |
| `scrape_website` | Scrape a website with Playwright |
| `verify_emails` | Verify unverified emails in the database |
| `get_leads` | Query leads with filters |
| `get_stats` | Lead count statistics |

## Required API keys

| Key | Service | Free tier | Get it at |
|-----|---------|-----------|-----------|
| `apify_token` | Apify (Google Maps + LinkedIn) | $5/month | https://console.apify.com/account/integrations |
| `anthropic_api_key` | Claude (AI strategist) | Pay-as-you-go | https://console.anthropic.com/settings/keys |

## Optional

| Key | Service | Notes |
|-----|---------|-------|
| `reacher_api_key` | Reacher hosted API | Paid. Prefer Docker instead (free). |
| `million_verifier_api_key` | MillionVerifier | Catch-all detection. Optional. |

## Email verification

Reacher runs as a free Docker container. Suplex auto-starts it when needed.
If Docker is not installed, ask the user to install Docker Desktop.

## Config

Stored at `~/.config/suplex/config.toml`. Set via `suplex setup` or the `configure` MCP tool.

## Data

SQLite database at `~/.config/suplex/suplex.db`. Query via `suplex leads` or `get_leads` MCP tool.
