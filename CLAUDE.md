@AGENTS.md

## Claude Code

- Use the suplex MCP server for all scraping/enrichment operations.
- For long-running jobs (state/country-level scrapes), use `run_background_job` or `run_batch_job` and poll with `get_job_status` / `get_batch_status`.
- Read the `clean_text` field from lead data to understand what a business does — you are the AI layer, no separate LLM call needed.
- When the user describes multiple search queries or sources, decompose into a batch job.
- Default to Playwright for Google Maps (free). Only use Apify when the user asks for scale or speed.
