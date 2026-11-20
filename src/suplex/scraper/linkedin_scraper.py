from __future__ import annotations

import logging
import os
from typing import Any

from suplex.models import Lead, LeadSource, ScrapeTarget, VerificationStatus
from suplex.scraper.apify_runner import ApifyError, run_actor
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

LINKEDIN_ACTOR = "harvestapi/linkedin-profile-search"

MODES = {
    "short": "Short",
    "full": "Full",
    "full_with_email": "Full + email search",
}

SEGMENT_COUNTRIES = {
    "US", "IN", "BR", "CN", "GB", "FR", "CA", "ID", "MX", "IT", "ES", "AU",
    "DE", "TR", "NL", "PH", "CO", "AR", "ZA", "CL", "MY", "NG", "AE", "EG",
    "BE", "SE", "SA", "PL", "PT", "CH", "KR", "DK", "RO", "SG", "JP", "TW",
    "IE", "KE", "NZ", "IL", "NO", "AT", "FI",
}


GENERIC_TITLES = {
    "owner", "founder", "manager", "director", "ceo", "cto", "cfo", "coo",
    "president", "vp", "vice president", "partner", "associate", "lead",
    "head", "chief", "executive", "administrator", "coordinator", "supervisor",
    "officer", "specialist", "consultant", "analyst", "engineer", "developer",
}


def _warn_generic_titles(titles: list[str], query: str) -> None:
    for title in titles:
        words = set(title.lower().split())
        if words and words.issubset(GENERIC_TITLES):
            logger.warning(
                "Generic title '%s' will match ALL %s in the location, not just '%s'. "
                "Use profession-specific titles like 'Owner Dentist' instead of 'Owner'. "
                "This WILL waste Apify credits on irrelevant results.",
                title, title.lower() + "s", query,
            )


class LinkedInScraper:
    def __init__(self, token: str | None = None, store: LeadStore | None = None) -> None:
        self.token = token or os.environ.get("APIFY_TOKEN", "")
        self.store = store or LeadStore()

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        if not self.token:
            raise ApifyError(
                "LinkedIn scraping needs an Apify token. Run `suplex setup` or set APIFY_TOKEN."
            )

        query = target.query
        location = target.location
        extra = target.extra
        max_results = target.max_results

        if extra.get("li_resolved"):
            li_locations = [location] if location else []
        else:
            li_locations = self._resolve_locations(location) if location else []

        run_input = self._build_input(query, li_locations, max_results, extra)

        logger.info("LinkedIn scrape: '%s' in %s → %s", query, location, li_locations[:3])

        items = await run_actor(self.token, LINKEDIN_ACTOR, run_input)
        leads = [self._to_lead(item, query, location) for item in items]

        logger.info("LinkedIn: %d profiles from '%s' in '%s'", len(leads), query, location)
        return leads

    def _resolve_locations(self, location: str | None) -> list[str]:
        if not location:
            return []

        suggestions = self.store.get_linkedin_location(location)
        if suggestions:
            return [suggestions[0]]

        city = location.split(",")[0].strip()
        if city != location:
            suggestions = self.store.get_linkedin_location(city)
            if suggestions:
                return [suggestions[0]]

        logger.debug("No cached LinkedIn location for '%s' — passing raw", location)
        return [location]

    @staticmethod
    def _build_input(
        query: str,
        li_locations: list[str],
        max_results: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        mode = extra.get("mode", "full_with_email")

        run_input: dict[str, Any] = {
            "profileScraperMode": MODES.get(mode, "Short"),
            "startPage": extra.get("start_page", 1),
        }

        max_pages = extra.get("max_pages", 100)
        run_input["takePages"] = min(max_pages, 100)

        if query:
            run_input["searchQuery"] = query

        if li_locations:
            run_input["locations"] = li_locations[:70]

        if extra.get("job_titles"):
            _warn_generic_titles(extra["job_titles"], query)
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

        if extra.get("current_companies"):
            run_input["currentCompanies"] = extra["current_companies"]
        if extra.get("past_companies"):
            run_input["pastCompanies"] = extra["past_companies"]
        if extra.get("company_headcount"):
            run_input["companyHeadcount"] = extra["company_headcount"]
        if extra.get("company_hq_locations"):
            run_input["companyHeadquarterLocations"] = extra["company_hq_locations"]

        if extra.get("industries"):
            run_input["industryIds"] = [str(i) for i in extra["industries"]]

        if extra.get("schools"):
            run_input["schools"] = extra["schools"]

        if extra.get("profile_languages"):
            run_input["profileLanguages"] = extra["profile_languages"]
        if extra.get("recently_changed_jobs"):
            run_input["recentlyChangedJobs"] = True

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

        if extra.get("auto_segment"):
            run_input["autoQuerySegmentation"] = True
            levels = extra.get("auto_segment_levels", ["default"])
            run_input["autoQuerySegmentationLevels"] = levels
            if extra.get("auto_segment_countries"):
                run_input["autoQuerySegmentationTargetCountries"] = [
                    c for c in extra["auto_segment_countries"] if c in SEGMENT_COUNTRIES
                ]
                run_input.pop("locations", None)

        if max_results > 0:
            run_input["maxItems"] = max_results

        return run_input

    @staticmethod
    def _to_lead(item: dict[str, Any], query: str, location: str | None) -> Lead:
        first = item.get("firstName", "") or ""
        last = item.get("lastName", "") or ""
        name = f"{first} {last}".strip() or item.get("fullName")

        current_company = ""
        current_title = item.get("headline", "")
        positions = item.get("currentPosition") or item.get("currentPositions") or []
        if isinstance(positions, list) and positions:
            pos = positions[0]
            current_company = pos.get("companyName", "")
        if not current_company:
            exp = item.get("experience") or []
            if isinstance(exp, list) and exp:
                current_company = exp[0].get("companyName", "")
                if not current_title or current_title == item.get("headline", ""):
                    current_title = exp[0].get("position", "") or current_title

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
        if not email:
            email = item.get("email") or item.get("personalEmail", "")

        raw_loc = item.get("location") or item.get("locationText", "")
        if isinstance(raw_loc, dict):
            lead_location = raw_loc.get("linkedinText", "") or raw_loc.get("default", "") or ""
        else:
            lead_location = raw_loc or ""

        public_id = item.get("publicIdentifier") or item.get("public_identifier", "")
        li_url = item.get("linkedinUrl") or item.get("linkedin_url", "")
        if not li_url and public_id:
            li_url = f"https://www.linkedin.com/in/{public_id}"

        websites = item.get("companyWebsites") or item.get("websites") or []
        website = ""
        if isinstance(websites, list) and websites:
            w = websites[0]
            website = w.get("url", "") if isinstance(w, dict) else str(w)

        phone = item.get("phone") or item.get("phoneNumber", "")

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
