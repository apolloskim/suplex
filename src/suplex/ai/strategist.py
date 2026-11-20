from __future__ import annotations

import json
import logging
import os

import anthropic

from suplex.models import LeadSource, ScrapePlan, ScrapeTarget

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are an expert Sales Development Representative (SDR) strategist.
Given a user's business domain and target customer preferences, you generate
a concrete scrape plan: what to search for on Google Maps and LinkedIn,
and which company websites to crawl directly.

Your goal is to find the highest-quality leads — decision-makers at companies
that are a strong fit for the user's product/service.

Output a JSON object with this exact schema:
{
  "reasoning": "Brief explanation of your strategy",
  "targets": [
    {
      "source": "google_maps" | "linkedin" | "playwright",
      "query": "search query or URL",
      "location": "optional location filter",
      "max_results": 100,
      "extra": {}
    }
  ],
  "schedule_cron": "optional cron expression for recurring scrapes, e.g. '0 9 * * 1' for Monday 9am"
}

Guidelines:
- For Google Maps: use specific business category + location queries
- For LinkedIn: use keyword queries (boolean AND / OR / NOT are supported), never search URLs
- For Playwright: use specific company website URLs (about/team/contact pages)
- Diversify across sources for better coverage
- Prioritize decision-maker titles: CEO, CTO, VP, Director, Head of, Owner, Founder
- Consider the user's industry to find adjacent/complementary businesses
"""


DEFAULT_MODEL = "claude-sonnet-5-5"


class StrategistError(RuntimeError):
    pass


class Strategist:
    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL) -> None:
        self.client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""),
        )
        self.model = model

    async def plan(
        self,
        *,
        domain: str,
        preferences: str = "",
        max_targets: int = 10,
    ) -> ScrapePlan:
        user_message = f"""\
My business: {domain}

Additional preferences: {preferences or "None specified"}

Generate a scrape plan with up to {max_targets} targets. Focus on finding
decision-makers who would be ideal customers for my product/service.
"""

        logger.info("Generating scrape plan for domain: %s", domain)

        response = await self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        if response.stop_reason == "refusal":
            raise StrategistError("The model declined to generate a scrape plan for this request")

        text = "".join(block.text for block in response.content if block.type == "text")
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        elif "```" in text:
            text = text.split("```")[1].split("```")[0]

        try:
            data = json.loads(text.strip())
        except json.JSONDecodeError as exc:
            raise StrategistError(
                f"The model did not return a JSON plan (stop_reason={response.stop_reason})"
            ) from exc

        targets = [
            ScrapeTarget(
                source=LeadSource(t["source"]),
                query=t["query"],
                location=t.get("location"),
                max_results=t.get("max_results", 100),
                extra=t.get("extra", {}),
            )
            for t in data["targets"]
        ]

        return ScrapePlan(
            reasoning=data["reasoning"],
            targets=targets,
            schedule_cron=data.get("schedule_cron"),
        )
