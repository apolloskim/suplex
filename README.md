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
- **Tile** across 241 countries — "Texas" becomes 1,025 city-level scrapes automatically
- **Background jobs** with progress tracking, crash recovery, and batch execution
- **MCP server** with 20 tools for AI agents (Claude Code, Codex, Copilot, Cursor)

## Quick start

```bash
suplex setup                                              # one-time setup
suplex scrape maps "dentist" -l "Austin, TX" -s playwright  # scrape Google Maps (free)
suplex enrich                                               # enrich all leads
suplex leads                                                # view results
```

## For AI agents

Suplex works as an MCP server and ships an agent skill:

```bash
suplex serve    # start MCP server (stdio)
```

Or agents call it via CLI commands directly. See [SKILL.md](skills/suplex/SKILL.md) for the full agent workflow.
