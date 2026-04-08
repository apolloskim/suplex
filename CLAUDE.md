@AGENTS.md

## Claude Code

- Use the suplex MCP server for all scraping/enrichment operations.
- **Always check `get_apify_budget` before launching Apify jobs** (LinkedIn, Google Maps with -s apify).
- For long-running jobs, use `run_background_job` or `run_batch_job` and poll with `get_job_status` / `get_batch_status`.
- Read `clean_text` and `raw_data` from leads — you are the AI layer, no separate LLM call needed.
- LinkedIn leads have pre-verified emails, rich profile data (experience, education, skills, about), and posts (after enrichment). Use these for personalization.
- When the user describes multiple queries/sources, decompose into a batch job.
- Default to Playwright for Google Maps (free). Only use Apify when user asks for scale or speed.
- Google Maps leads include reviews (default 5 per place) — use for conversation hooks.
