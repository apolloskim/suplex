@AGENTS.md

## Claude Code

- Use the suplex MCP server for all scraping/enrichment operations.
- **Always check `get_apify_budget` before Apify jobs.** If over budget, use Playwright for Maps.
- **Always pass `-n` for testing.** Never exhaust without user confirmation.
- For multiple queries/sources, use `run_batch_job` — watchdog auto-advances, no polling needed.
- Scraping auto-enriches by default. Use `--no-enrich` only when user wants scrape-only.
- Read `raw_data` and `enrichment_data` from leads — you are the AI layer.
- LinkedIn leads have pre-verified emails + full profile (experience, education, skills, about, posts after enrichment).
- Google Maps leads have reviews (default 5 per place) — use for conversation hooks.
- For killed/failed jobs, use `resume_job` to pick up where it left off.
