# suplex

Lead scraping, enrichment and email verification, as a CLI and an MCP server for AI agents.

## Install

Requires Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/apolloskim/suplex.git
cd suplex
uv sync
uv run playwright install chromium
uv run suplex --help
```

## What it does

- Scrapes business leads from Google Maps, with a local Playwright browser or an Apify actor.
- Scrapes people from LinkedIn through an Apify actor.
- Splits a state or country into one job per city, and resumes a job that stopped or failed.
- Enriches each lead from its website, then finds and verifies an email address.
- Tracks outreach campaigns and sends through the Google Workspace CLI.
- Exposes all of it as MCP tools.

## Try it

These need no accounts:

```bash
uv run suplex regions Japan
uv run suplex regions --search Rotterdam
uv run suplex stats
uv run suplex jobs
```

Scraping, enrichment and outreach call live services. Keys are read from the environment: `APIFY_TOKEN` for Apify and `ANTHROPIC_API_KEY` for the planner. To see which are set:

```bash
uv run suplex doctor
```

Each command lists its own options:

```bash
uv run suplex scrape maps --help
uv run suplex scrape linkedin --help
uv run suplex outreach --help
```

## For AI agents

```bash
uv run suplex serve
```

This starts the MCP server on stdio. [AGENTS.md](AGENTS.md) is the guide for agents.

## Development

```bash
uv run ruff check src
uv run pyright
```

## Background

Suplex started as a personal project. The company behind the idea has stopped, but the know-how remained, and offering it as an MCP server gets close to what we wanted to provide: something people play with themselves and turn into a setup that fits them, with whichever AI they prefer, whether Claude, GPT, Gemini or an open model. I built it in under a week and enjoyed it. I haven't run it in a while and don't plan to soon, since I have no one to cold email.
