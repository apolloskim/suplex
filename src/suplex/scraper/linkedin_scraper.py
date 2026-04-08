"""LinkedIn scraper via Apify — uses harvestapi/linkedin-profile-search actor.

Location handling:
- Locations are resolved to LinkedIn's canonical autocomplete format via SQLite cache
- The actor types the location into LinkedIn's autocomplete and picks the first match
- Cached autocomplete strings are more reliable than raw text
- Up to 70 locations per request
- For broad searches (state/country), use autoQuerySegmentation

Auto query segmentation (bypasses 2500-result limit):
- Segments: country, state, seniority_level, industry
- Target countries: US, IN, BR, CN, GB, FR, CA, etc. (43 supported)
- When enabled with target countries, omit locations to let Apify segment
"""

from __future__ import annotations

import logging
import os
from typing import Any

from apify_client import ApifyClientAsync

from suplex.models import Lead, LeadSource, ScrapeTarget
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

LINKEDIN_ACTOR = "harvestapi/linkedin-profile-search"

MODES = {
    "short": "Short",
    "full": "Full",
    "full_with_email": "Full + email search",
}

# Countries that support auto query segmentation.
SEGMENT_COUNTRIES = {
    "US", "IN", "BR", "CN", "GB", "FR", "CA", "ID", "MX", "IT", "ES", "AU",
    "DE", "TR", "NL", "PH", "CO", "AR", "ZA", "CL", "MY", "NG", "AE", "EG",
    "BE", "SE", "SA", "PL", "PT", "CH", "KR", "DK", "RO", "SG", "JP", "TW",
    "IE", "KE", "NZ", "IL", "NO", "AT", "FI",
}


class LinkedInScraper:
    """Scrapes LinkedIn people search via Apify's HarvestAPI actor."""

    def __init__(self, token: str | None = None, store: LeadStore | None = None) -> None:
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.store = store or LeadStore()

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        if not self.token:
            logger.error("No APIFY_TOKEN — cannot scrape LinkedIn")
            return []

        query = target.query
        location = target.location
        extra = target.extra
        max_results = target.max_results

        # If tile planner already resolved the autocomplete, use it directly.
        if extra.get("li_resolved"):
            li_locations = [location] if location else []
        else:
            li_locations = self._resolve_locations(location) if location else []

        run_input = self._build_input(query, li_locations, max_results, extra)

        logger.info("LinkedIn scrape: '%s' in %s → %s", query, location, li_locations[:3])

        items = await self._run_actor(run_input)
        leads = [self._to_lead(item, query, location) for item in items]

        logger.info("LinkedIn: %d profiles from '%s' in '%s'", len(leads), query, location)
        return leads

    def _resolve_locations(self, location: str | None) -> list[str]:
        """Resolve a location to LinkedIn's canonical autocomplete format(s)."""
        if not location:
            return []

        # Try exact match in autocomplete cache.
        suggestions = self.store.get_linkedin_location(location)
        if suggestions:
            # Return the first (most relevant) suggestion.
            return [suggestions[0]]

        # Try city name only (strip ", TX" etc).
        city = location.split(",")[0].strip()
        if city != location:
            suggestions = self.store.get_linkedin_location(city)
            if suggestions:
                return [suggestions[0]]

        # Fallback: pass through raw. The actor will attempt its own autocomplete.
        logger.debug("No cached LinkedIn location for '%s' — passing raw", location)
        return [location]

    @staticmethod
    def _build_input(
        query: str,
        li_locations: list[str],
        max_results: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        """Build the Apify actor run_input from the schema."""
        mode = extra.get("mode", "full_with_email")

        run_input: dict[str, Any] = {
            "profileScraperMode": MODES.get(mode, "Short"),
            "startPage": extra.get("start_page", 1),
        }

        # Pagination: takePages max 100 (= 2500 profiles).
        max_pages = extra.get("max_pages", 100)
        run_input["takePages"] = min(max_pages, 100)

        # Fuzzy search query.
        if query:
            run_input["searchQuery"] = query

        # Locations (up to 70).
        if li_locations:
            run_input["locations"] = li_locations[:70]

        # Job filters.
        if extra.get("job_titles"):
            run_input["currentJobTitles"] = extra["job_titles"]
        if extra.get("past_job_titles"):
            run_input["pastJobTitles"] = extra["past_job_titles"]
        if extra.get("seniority_levels"):
            run_input["seniorityLevelIds"] = [str(s) for s in extra["seniority_levels"]]
        if extra.get("functions"):
            run_input["functionIds"] = [str(f) for f in extra["functions"]]
        if extra.get("years_experience"):
            run_input["yearsOfExperienceIds"] = [str(y) for y in extra["years_experience"]]
        if extra.get("years_at_company"):
            run_input["yearsAtCurrentCompanyIds"] = [str(y) for y in extra["years_at_company"]]

        # Company filters.
        if extra.get("current_companies"):
            run_input["currentCompanies"] = extra["current_companies"]
        if extra.get("past_companies"):
            run_input["pastCompanies"] = extra["past_companies"]
        if extra.get("company_headcount"):
            run_input["companyHeadcount"] = extra["company_headcount"]
        if extra.get("company_hq_locations"):
            run_input["companyHeadquarterLocations"] = extra["company_hq_locations"]

        # Industry.
        if extra.get("industries"):
            run_input["industryIds"] = [str(i) for i in extra["industries"]]

        # Education.
        if extra.get("schools"):
            run_input["schools"] = extra["schools"]

        # Profile filters.
        if extra.get("profile_languages"):
            run_input["profileLanguages"] = extra["profile_languages"]
        if extra.get("recently_changed_jobs"):
            run_input["recentlyChangedJobs"] = True

        # Exclude filters.
        _EXCLUDE_MAP = {
            "exclude_locations": "excludeLocations",
            "exclude_companies": "excludeCurrentCompanies",
            "exclude_past_companies": "excludePastCompanies",
            "exclude_titles": "excludeCurrentJobTitles",
            "exclude_past_titles": "excludePastJobTitles",
            "exclude_industries": "excludeIndustryIds",
            "exclude_schools": "excludeSchools",
            "exclude_seniority": "excludeSeniorityLevelIds",
            "exclude_functions": "excludeFunctionIds",
            "exclude_company_hq": "excludeCompanyHeadquarterLocations",
        }
        for our_key, apify_key in _EXCLUDE_MAP.items():
            if extra.get(our_key):
                run_input[apify_key] = [str(v) for v in extra[our_key]]

        # Auto query segmentation (bypasses 2500-result limit).
        if extra.get("auto_segment"):
            run_input["autoQuerySegmentation"] = True
            levels = extra.get("auto_segment_levels", ["default"])
            run_input["autoQuerySegmentationLevels"] = levels
            if extra.get("auto_segment_countries"):
                run_input["autoQuerySegmentationTargetCountries"] = [
                    c for c in extra["auto_segment_countries"] if c in SEGMENT_COUNTRIES
                ]
                # When using country segmentation, omit locations per actor docs.
                run_input.pop("locations", None)

        # Result limit.
        if max_results > 0:
            run_input["maxItems"] = max_results

        return run_input

    async def _run_actor(self, run_input: dict[str, Any]) -> list[dict[str, Any]]:
        """Run the LinkedIn actor and collect all results."""
        client = ApifyClientAsync(self.token)
        run = await client.actor(LINKEDIN_ACTOR).call(run_input=run_input)
        dataset_id = run["defaultDatasetId"]
        items_page = await client.dataset(dataset_id).list_items()
        items = items_page.items
        logger.info("LinkedIn actor returned %d profiles", len(items))
        return items

    @staticmethod
    def _to_lead(item: dict[str, Any], query: str, location: str | None) -> Lead:
        """Convert an Apify LinkedIn profile to a Lead with all SDR-relevant signals."""
        first = item.get("firstName", "") or ""
        last = item.get("lastName", "") or ""
        name = f"{first} {last}".strip() or item.get("fullName")

        # Company + title from currentPosition (full mode returns list).
        current_company = ""
        current_title = item.get("headline", "")
        positions = item.get("currentPosition") or item.get("currentPositions") or []
        if isinstance(positions, list) and positions:
            pos = positions[0]
            current_company = pos.get("companyName", "")
            # Title from experience is more specific than headline.
        # Fallback to experience.
        if not current_company:
            exp = item.get("experience") or []
            if isinstance(exp, list) and exp:
                current_company = exp[0].get("companyName", "")
                if not current_title or current_title == item.get("headline", ""):
                    current_title = exp[0].get("position", "") or current_title

        # Email — from emails[] array (pre-verified by Apify, no re-verification needed).
        email = ""
        email_status = ""
        emails_list = item.get("emails") or []
        if isinstance(emails_list, list) and emails_list:
            best = emails_list[0]
            if isinstance(best, dict):
                email = best.get("email", "")
                email_status = best.get("status", "")
            elif isinstance(best, str):
                email = best
        # Fallback to flat email field.
        if not email:
            email = item.get("email") or item.get("personalEmail", "")

        # Location — structured.
        raw_loc = item.get("location") or item.get("locationText", "")
        if isinstance(raw_loc, dict):
            lead_location = raw_loc.get("linkedinText", "") or raw_loc.get("default", "") or ""
        else:
            lead_location = raw_loc or ""

        # LinkedIn URL.
        public_id = item.get("publicIdentifier") or item.get("public_identifier", "")
        li_url = item.get("linkedinUrl") or item.get("linkedin_url", "")
        if not li_url and public_id:
            li_url = f"https://www.linkedin.com/in/{public_id}"

        # Website — from companyWebsites[].
        websites = item.get("companyWebsites") or item.get("websites") or []
        website = ""
        if isinstance(websites, list) and websites:
            w = websites[0]
            website = w.get("url", "") if isinstance(w, dict) else str(w)

        # Phone.
        phone = item.get("phone") or item.get("phoneNumber", "")

        # Verification status — LinkedIn emails are pre-verified by Apify.
        from suplex.models import VerificationStatus
        verification = VerificationStatus.PENDING
        if email and email_status:
            if email_status in ("valid", "safe"):
                verification = VerificationStatus.VALID
            elif email_status == "risky":
                verification = VerificationStatus.CATCH_ALL
            elif email_status == "invalid":
                verification = VerificationStatus.INVALID

        return Lead(
            name=name,
            email=email or None,
            company=current_company,
            title=current_title,
            phone=phone or None,
            location=lead_location,
            linkedin_url=li_url,
            website=website or None,
            source=LeadSource.LINKEDIN,
            verification_status=verification,
            raw_data={
                **item,
                "search_query": query,
                "search_location": location,
            },
        )
