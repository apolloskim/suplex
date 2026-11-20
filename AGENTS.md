# Suplex

Lead scraping, enrichment and email verification, as a CLI and an MCP server.

## Working on this repository

- Documentation is `README.md` and this file. `CLAUDE.md` only imports this file.
- No code comments and no docstrings. The exceptions are behaviour, not documentation: the docstring of a `click` command is its `--help` text, and MCP tool descriptions are sent to agents.
- No unit tests. The checks are `uv run ruff check src`, `uv run pyright` and running the real CLI.
- `src/suplex/skills/suplex/SKILL.md` is shipped in the package and installed by `suplex setup`. It is the only copy.
- Never commit API keys or tokens. Never edit the geo data files by hand.
- Do not send real requests (scrape, email, Apify) without the owner's approval.

## Commands

```bash
suplex setup          # install skill, register MCP server, Playwright, API keys
suplex doctor         # check services
suplex budget         # check Apify credits

suplex regions Japan                     # list regions with city counts
suplex regions --search Rotterdam        # find region, country and LinkedIn autocompletes

suplex scrape maps "query" -l "location" -s playwright|apify -n 20 -r 5
suplex scrape maps "query" -l US --only CA,TX,NY -n 20
suplex scrape maps "query" -l US --exclude WY,VT -n 20
suplex scrape linkedin "query AND keyword" -l "location" --seniority 320 --functions 11 -n 20
suplex scrape website https://example.com

suplex run scrape-maps "query" -l "location" -s playwright -n 20
suplex run scrape-linkedin "query AND keyword" -l "location" -n 20
suplex run enrich
suplex run watch-reddit --subreddit name
suplex run revisit-reddit
suplex run hydrate-reddit

suplex enrich --linkedin-posts 1
suplex verify -n 50

suplex jobs
suplex job <id>
suplex resume <id>
suplex stop <id>

suplex leads --source google_maps --status valid
suplex stats
suplex export -o leads.csv

suplex playbook init
suplex mailbox add user@gmail.com
suplex campaign create "name" -m <mailbox_id> -a "123 Main St, City, ST 12345"
suplex outreach preview <campaign_id> -n 5
suplex outreach drafts <campaign_id>
suplex outreach approve --all -c <campaign_id>
suplex outreach send <campaign_id> -n 10
suplex outreach replies <mailbox_id>
suplex outreach due <campaign_id>

suplex profile <url>
suplex plan "business description"
suplex serve          # MCP server over stdio
```

## Rules for agents using Suplex

- Use the suplex MCP server for scraping and enrichment.
- Always pass `-n` for max results. Never exhaust a search without the user's confirmation.
- Check `get_apify_budget` before Apify jobs. Over-budget tiles are skipped and the job ends as `done_with_errors`.
- Scraping enriches afterwards by default; `--no-enrich` skips it.
- A lead's scraped fields are in `raw_data`; enrichment results are in `enrichment_data`.
- LinkedIn emails arrive already verified by the Apify actor and are not re-verified.
- LinkedIn posts are fetched only for leads that have an email.
- For several queries or sources use `run_batch_job`; a watchdog starts queued jobs as slots open.
- `resume_job` continues a killed, failed or `done_with_errors` job: finished tiles are skipped, failed tiles retried.
- For country-wide scrapes, check `suplex jobs` for finished regions and pass `--exclude`.
- `suplex regions --search <name>` finds the local region names that `--only` and `--exclude` match.
- Never send outreach before the user has reviewed the drafts.

## Data model

- **leads**: deduplicated by `linkedin_url`, then `google_maps_url`, then email + source, then name + location + source.
- **jobs**: one row per background job; a batch is a parent row with child rows. Status is `pending`, `running`, `done`, `done_with_errors`, `failed` or `stopped`.
- **item_tasks**: one row per tile of a scrape job, which is what makes a job resumable.
- **cities**, **linkedin_geo_map**: bundled geo data, loaded on first use.
- **sender_profile**: the user's own business, one row.
- **sender_mailboxes**, **campaigns**, **outreach**, **suppression_list**: outreach state.
- **reddit_posts**, **reddit_post_snapshots**: Reddit watch state.

## Architecture

- `suplex.cli` and `suplex.mcp_server` are the two entry points over the same modules.
- `scraper.coordinator` expands a target into tiles (`geo.tiles`), runs them with concurrency limits and records each tile in `item_tasks`.
- Google Maps runs through a local Playwright browser (`scraper.gmaps_scraper`) or an Apify actor (`scraper.apify_scraper`), chosen by `-s`. LinkedIn always runs through Apify (`scraper.linkedin_scraper`).
- `enrich.pipeline` scrapes the lead's website, finds and verifies an email, and optionally fetches LinkedIn posts.
- `verify.verifier` checks syntax, then Reacher (local Docker or hosted), then MillionVerifier for catch-all domains.
- `jobs` launches each background job as `python -m suplex ...` in its own session and tracks it by pid.
- `outreach.engine` sends through the Google Workspace CLI (`gws`), one config directory per mailbox.
- Suplex returns data; the calling agent does the interpretation and writes the emails.
