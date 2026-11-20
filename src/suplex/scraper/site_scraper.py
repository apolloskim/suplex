"""Site scraper — two-tier: httpx+selectolax (fast) → Playwright (JS fallback).

Extracts rich business data for two purposes:
1. Sender profile — understand who the sender is, their value prop, audience
2. Lead enrichment — understand the lead's business for personalized outreach

Data extracted:
- Identity: name, type, description, value proposition
- Contact: emails, phones, address, social links
- Business: hours, services, pricing signals, team, reviews/testimonials
- Content: blog posts, FAQ, case studies, customer logos
- Technical: tech stack, navigation structure
- Signals: hiring, company size, competitors/tools mentioned
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urljoin, urlparse

import extruct
import httpx
import trafilatura
from selectolax.lexbor import LexborHTMLParser

from suplex.models import Lead, LeadSource, ScrapeTarget
from suplex.urls import normalize_url, strip_www

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(
    r"(?:\+?1[-.\s]?)?"
    r"(?:\([0-9]{3}\)|[0-9]{3})"
    r"[-.\s]?[0-9]{3}"
    r"[-.\s]?[0-9]{4}"
)
SOCIAL_DOMAINS = {
    "linkedin.com", "twitter.com", "x.com", "facebook.com",
    "instagram.com", "youtube.com", "tiktok.com", "github.com",
}
SKIP_EMAILS = {
    "noreply", "no-reply", "example.com", "sentry.io", "wixpress.com",
    "googleapis.com", "yoursite.com", "yourdomain.com", "email.com",
    ".min.js", ".bundle.js", ".chunk.js", ".png", ".jpg", ".svg",
}
CONTACT_SLUGS = {"contact", "about", "team", "staff", "our-team", "about-us", "contact-us"}
HIGH_VALUE_SLUGS = {
    "about", "about-us", "team", "our-team", "staff",
    "contact", "contact-us",
    "services", "solutions", "products", "what-we-do",
    "pricing", "plans",
    "blog", "news", "insights",
    "faq", "faqs", "frequently-asked-questions",
    "testimonials", "reviews", "case-studies", "customers",
    "careers", "jobs", "hiring",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# JS detection heuristics
# ---------------------------------------------------------------------------

_JS_ROOT_RE = re.compile(
    r'<div\s+id="(?:root|app|__next|__nuxt|___gatsby)"[^>]*>\s*</div>',
    re.IGNORECASE,
)
_NOSCRIPT_RE = re.compile(
    r"<noscript[^>]*>.*?(?:enable\s+javascript|javascript\s+is\s+required"
    r"|this\s+app\s+requires|you\s+need\s+to\s+enable).*?</noscript>",
    re.IGNORECASE | re.DOTALL,
)
_JS_BUNDLE_RE = re.compile(
    r"(?:_next/static|/_nuxt/|/static/js/|chunk\.[a-f0-9]+\.js"
    r"|bundle\.[a-f0-9]+\.js|app\.[a-f0-9]+\.js)",
    re.IGNORECASE,
)


def needs_js_rendering(html: str) -> bool:
    """Heuristic: does this HTML look like a JS-rendered shell?"""
    tree = LexborHTMLParser(html)
    body = tree.css_first("body")
    body_text = body.text().strip() if body else ""
    text_len = len(body_text)
    html_len = len(html)

    # Very short body text but non-trivial HTML.
    if html_len > 2000 and text_len < 100:
        return True

    # Text-to-HTML ratio below 5%.
    if html_len > 5000 and (text_len / html_len) < 0.05:
        return True

    # Empty framework root div.
    if _JS_ROOT_RE.search(html):
        return True

    # Noscript warning.
    if _NOSCRIPT_RE.search(html):
        return True

    # Script-heavy with thin content.
    script_bytes = sum(len(s.text()) for s in tree.css("script") if s.text())
    if script_bytes > 0 and text_len < 200 and script_bytes > html_len * 0.6:
        return True

    # Multiple JS bundles + thin content.
    if len(_JS_BUNDLE_RE.findall(html)) >= 2 and text_len < 500:
        return True

    return False


# ---------------------------------------------------------------------------
# Site data model
# ---------------------------------------------------------------------------

class SiteData:
    """All structured data extracted from a site."""

    def __init__(self) -> None:
        # Identity
        self.url: str = ""
        self.title: str = ""
        self.description: str = ""
        self.value_proposition: str = ""
        self.business_name: str = ""
        self.business_type: str = ""

        # Contact
        self.emails: list[str] = []
        self.phones: list[str] = []
        self.address: str = ""
        self.social_links: dict[str, str] = {}

        # Business
        self.hours: list[dict] = []
        self.services: list[str] = []
        self.pricing_signals: list[str] = []
        self.team_members: list[dict] = []
        self.testimonials: list[dict] = []
        self.faq: list[dict] = []
        self.customer_logos: list[str] = []
        self.case_studies: list[str] = []

        # Content
        self.blog_posts: list[dict] = []
        self.headings: list[str] = []
        self.nav_links: list[str] = []
        self.cta_texts: list[str] = []

        # Signals
        self.tech_stack: list[str] = []
        self.hiring: list[str] = []
        self.tools_mentioned: list[str] = []
        self.company_size_signals: list[str] = []

        # Structured
        self.json_ld: list[dict] = []
        self.opengraph: dict[str, str] = {}

        # Internal (not exported)
        self.raw_text: str = ""
        self._links: list[dict] = []
        self._clean_texts: list[str] = []  # Trafilatura output per page

    def to_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if v and not k.startswith("_")}
        d.pop("raw_text", None)
        # Include clean text for the agent to interpret.
        if self._clean_texts:
            d["clean_text"] = "\n\n---\n\n".join(self._clean_texts)[:6000]
        return d


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------

class SiteScraper:
    """Two-tier site scraper: fast HTTP first, Playwright fallback for JS shells."""

    async def scrape(self, target: ScrapeTarget) -> list[Lead]:
        url = normalize_url(target.query)
        logger.info("Scraping site: %s", url)

        # Tier 1: Fast HTTP.
        site_data, raw_html = await self._scrape_fast(url)

        # Smart fallback: detect JS shell. If the browser tier fails (Chromium not
        # installed, navigation timeout), keep what the HTTP tier already found.
        if needs_js_rendering(raw_html):
            logger.info("JS shell detected — falling back to Playwright for %s", url)
            try:
                site_data, _ = await self._scrape_playwright(url)
            except Exception as exc:
                logger.warning("Playwright fallback failed for %s (%s) — using static HTML only", url, exc)

        # Discover and scrape high-value internal pages.
        internal_urls = self._find_high_value_urls(site_data)
        for page_url in internal_urls[:5]:
            try:
                extra, extra_html = await self._scrape_fast(page_url)
                if needs_js_rendering(extra_html):
                    extra, _ = await self._scrape_playwright(page_url)
                self._merge(site_data, extra)
            except Exception:
                logger.debug("Failed to scrape internal page: %s", page_url)

        return self._to_leads(site_data)

    # ------------------------------------------------------------------
    # Tier 1: httpx + selectolax + extruct
    # ------------------------------------------------------------------

    async def _scrape_fast(self, url: str) -> tuple[SiteData, str]:
        data = SiteData()
        data.url = url

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            html = resp.text

        self._parse_html(data, html, url)

        logger.info(
            "Tier 1: %s — %d emails, %d phones, %d JSON-LD, %d headings",
            url, len(data.emails), len(data.phones), len(data.json_ld), len(data.headings),
        )
        return data, html

    # ------------------------------------------------------------------
    # Tier 2: Playwright
    # ------------------------------------------------------------------

    async def _scrape_playwright(self, url: str) -> tuple[SiteData, str]:
        from playwright.async_api import async_playwright

        data = SiteData()
        data.url = url

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = await browser.new_context(
            user_agent=USER_AGENT, viewport={"width": 1280, "height": 720},
        )

        try:
            page = await context.new_page()
            await page.route(
                "**/*.{png,jpg,jpeg,gif,svg,woff,woff2}",
                lambda route: route.abort(),
            )

            # Capture API responses.
            api_data: list[dict] = []

            async def _on_response(response):
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "application/json" in ct:
                    try:
                        api_data.append(await response.json())
                    except Exception:
                        pass

            page.on("response", _on_response)

            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(3000)

            html = await page.content()
            self._parse_html(data, html, url)

            # Merge any captured API JSON.
            for item in api_data:
                if isinstance(item, dict):
                    data.json_ld.append(item)

            logger.info(
                "Tier 2: %s — %d emails, %d phones, %d api responses captured",
                url, len(data.emails), len(data.phones), len(api_data),
            )
            return data, html
        finally:
            await context.close()
            await browser.close()
            await pw.stop()

    # ------------------------------------------------------------------
    # Shared HTML parsing (used by both tiers)
    # ------------------------------------------------------------------

    def _parse_html(self, data: SiteData, html: str, url: str) -> None:
        tree = LexborHTMLParser(html)

        # --- Meta ---
        title_node = tree.css_first("title")
        data.title = data.title or (title_node.text().strip() if title_node else "")

        desc_node = tree.css_first('meta[name="description"]')
        data.description = data.description or (_attr(desc_node, "content") if desc_node else "")

        # --- Structured metadata (JSON-LD, OpenGraph) ---
        metadata = extruct.extract(html, base_url=url, syntaxes=["json-ld", "opengraph", "microdata"])
        data.json_ld.extend(metadata.get("json-ld", []))
        og_list = metadata.get("opengraph", [])
        if og_list and not data.opengraph:
            data.opengraph = og_list[0] if isinstance(og_list[0], dict) else {}

        self._parse_json_ld(data)

        # --- Body text (selectolax raw for regex extraction) ---
        body = tree.css_first("body")
        data.raw_text = body.text() if body else ""

        # --- Clean text via trafilatura (for LLM semantic extraction) ---
        clean = trafilatura.extract(html, output_format="txt", include_tables=True, favor_precision=False)
        if clean and len(clean) > 30:
            data._clean_texts.append(clean)

        # --- Business name fallback (OG, title) ---
        if not data.business_name:
            og_name = data.opengraph.get("og:site_name", "")
            if og_name:
                data.business_name = og_name
            elif data.title:
                # Strip common suffixes like " - Home", " | Official Site"
                data.business_name = re.split(r"\s*[|\-–—]\s*", data.title)[0].strip()

        # --- Headings ---
        for tag in ("h1", "h2", "h3"):
            for node in tree.css(tag):
                text = node.text().strip()
                if text and len(text) < 200 and text not in data.headings:
                    data.headings.append(text)

        # --- Navigation ---
        for nav in tree.css("nav a[href]"):
            text = nav.text().strip()
            if text and len(text) < 50 and text not in data.nav_links:
                data.nav_links.append(text)

        # --- Emails ---
        for mailto in tree.css('a[href^="mailto:"]'):
            email = _attr(mailto, "href").replace("mailto:", "").split("?")[0].strip()
            if email:
                data.emails.append(email)
        data.emails.extend(EMAIL_RE.findall(data.raw_text))
        data.emails.extend(EMAIL_RE.findall(html))
        data.emails = _clean_emails(data.emails)

        # --- Phones (prioritize tel: links, then regex) ---
        tel_phones: list[str] = []
        for tel in tree.css('a[href^="tel:"]'):
            phone = _attr(tel, "href").replace("tel:", "").strip()
            if phone:
                tel_phones.append(phone)
        regex_phones = PHONE_RE.findall(data.raw_text)
        # Tel: links are high confidence. Regex phones on e-commerce sites are noisy (SKUs).
        # Prioritize tel: links; only add regex matches if we have fewer than 3 tel: links.
        unique_tel = list(dict.fromkeys(tel_phones))
        if len(unique_tel) >= 3:
            data.phones = unique_tel[:5]
        else:
            regex_clean = [p.strip() for p in regex_phones if len(p.strip()) >= 10]
            data.phones = list(dict.fromkeys(unique_tel + regex_clean))[:5]

        # --- Address ---
        if not data.address:
            for addr_node in tree.css("address"):
                text = addr_node.text().strip()
                if text:
                    data.address = text
                    break

        # --- Social links ---
        for link in tree.css("a[href]"):
            href = _attr(link, "href")
            try:
                domain = strip_www(urlparse(href).netloc)
                if domain in SOCIAL_DOMAINS:
                    key = domain.split(".")[0]
                    if key not in data.social_links:
                        data.social_links[key] = href
            except Exception:
                pass

        # --- CTAs (skip generic nav/cart actions) ---
        _SKIP_CTA = {"skip to content", "checkout", "continue to checkout", "check out",
                      "sign in", "log in", "close", "accept", "dismiss", "x", "menu",
                      "search", "submit", "send", "cancel"}
        for selector in ("a.btn", "a.button", 'a[class*="cta"]', 'button[class*="cta"]'):
            for node in tree.css(selector):
                text = node.text().strip()
                if (text and 3 < len(text) < 60
                        and text.lower() not in _SKIP_CTA
                        and text not in data.cta_texts):
                    data.cta_texts.append(text)

        # --- Testimonials / reviews ---
        seen_testimonials: set[str] = {t.get("text", "")[:80] for t in data.testimonials}
        for selector in (
            '[class*="testimonial"]', '[class*="review"]', '[class*="quote"]',
            "blockquote", '[itemprop="review"]',
        ):
            for node in tree.css(selector):
                text = node.text().strip()
                key = text[:80]
                # Filter out CSS/HTML noise — real testimonials are mostly words.
                alpha_ratio = sum(c.isalpha() or c.isspace() for c in text) / max(len(text), 1)
                if text and len(text) > 30 and alpha_ratio > 0.7 and key not in seen_testimonials:
                    seen_testimonials.add(key)
                    data.testimonials.append({"text": text[:500]})
                    if len(data.testimonials) >= 10:
                        break

        # --- FAQ ---
        seen_faq: set[str] = {q["question"] for q in data.faq}
        for selector in ('[class*="faq"] details', '[itemtype*="FAQPage"] details',
                         '[class*="faq"] [class*="item"]', "details > summary"):
            for node in tree.css(selector):
                if selector.endswith("summary"):
                    # For bare <details><summary>, the node IS the summary.
                    q_text = node.text().strip()[:200]
                    parent = node.parent
                    a_parts = []
                    if parent:
                        for child in parent.iter():
                            if child.tag != "summary" and child.text():
                                a_parts.append(child.text().strip())
                    a_text = " ".join(a_parts)[:500]
                else:
                    q_node = node.css_first("summary, dt, [class*='question'], h3, h4")
                    a_node = node.css_first("dd, [class*='answer'], p")
                    q_text = q_node.text().strip()[:200] if q_node else ""
                    a_text = a_node.text().strip()[:500] if a_node else ""

                if q_text and len(q_text) > 5 and q_text not in seen_faq:
                    seen_faq.add(q_text)
                    data.faq.append({"question": q_text, "answer": a_text})

        # --- Blog posts / recent content ---
        for selector in (
            '[class*="blog"] article', '[class*="post"]',
            'article[class*="entry"]', '[class*="news-item"]',
        ):
            for node in tree.css(selector):
                title_el = node.css_first("h2, h3, a")
                date_el = node.css_first("time, [class*='date']")
                if title_el:
                    post: dict[str, str] = {"title": title_el.text().strip()[:200]}
                    link_el = node.css_first("a[href]")
                    if link_el:
                        post["url"] = urljoin(url, _attr(link_el, "href"))
                    if date_el:
                        post["date"] = date_el.text().strip()
                    data.blog_posts.append(post)

        # --- Pricing signals ---
        pricing_keywords = ["free trial", "free plan", "enterprise", "contact sales", "get a quote",
                            "per month", "/mo", "pricing", "starter", "professional", "premium"]
        text_lower = data.raw_text.lower()
        for kw in pricing_keywords:
            if kw in text_lower and kw not in data.pricing_signals:
                data.pricing_signals.append(kw)

        # --- Hiring signals ---
        hiring_keywords = ["we're hiring", "join our team", "open positions", "careers",
                           "job openings", "apply now"]
        for kw in hiring_keywords:
            if kw in text_lower and kw not in data.hiring:
                data.hiring.append(kw)

        # --- Company size signals ---
        size_patterns = [
            (r"\b(\d+)\+?\s*(?:employees|team members|people)", "employees"),
            (r"\b(\d+)\+?\s*(?:locations|offices|countries)", "locations"),
            (r"series\s+[a-d]", "funding"),
            (r"\$\d+[mb]\s*(?:raised|funding|valuation)", "funding"),
        ]
        for pattern, label in size_patterns:
            match = re.search(pattern, text_lower)
            if match:
                data.company_size_signals.append(f"{label}: {match.group()}")

        # --- Customer logos / case studies ---
        for selector in ('[class*="logo"]', '[class*="client"]', '[class*="partner"]', '[class*="trusted"]'):
            for node in tree.css(selector):
                for img in node.css("img[alt]"):
                    alt = _attr(img, "alt").strip()
                    if alt and len(alt) < 100 and alt not in data.customer_logos:
                        data.customer_logos.append(alt)

        # --- Tech stack ---
        for script in tree.css("script[src]"):
            src = _attr(script, "src").lower()
            for tech, patterns in _TECH_PATTERNS.items():
                if any(p in src for p in patterns) and tech not in data.tech_stack:
                    data.tech_stack.append(tech)
        gen = tree.css_first('meta[name="generator"]')
        if gen:
            val = _attr(gen, "content")
            if val and val not in data.tech_stack:
                data.tech_stack.append(val)

        # --- Competitor/tool mentions ---
        for node in tree.css('[class*="integration"]'):
            for img in node.css("img[alt]"):
                alt = _attr(img, "alt").strip()
                if alt and alt not in data.tools_mentioned:
                    data.tools_mentioned.append(alt)

        # --- Internal links for further crawling ---
        for a in tree.css("a[href]"):
            href = _attr(a, "href")
            text = a.text().strip().lower()
            data._links.append({"href": href, "text": text})

    # ------------------------------------------------------------------
    # JSON-LD parsing
    # ------------------------------------------------------------------

    @classmethod
    def _parse_json_ld(cls, data: SiteData) -> None:
        """Pull known fields out of JSON-LD blocks.

        JSON-LD is written by hand and by plugins, so any field can be the
        wrong type. One bad block must not abort the whole site scrape.
        """
        for item in list(data.json_ld):
            if not isinstance(item, dict):
                continue
            try:
                cls._parse_json_ld_item(data, item)
            except (AttributeError, TypeError, ValueError):
                logger.debug("Skipping malformed JSON-LD block on %s", data.url, exc_info=True)

    @staticmethod
    def _parse_json_ld_item(data: SiteData, item: dict[str, Any]) -> None:
        ld_type = str(item.get("@type", ""))

        # Business identity.
        if any(t in ld_type for t in ["LocalBusiness", "Organization", "Corporation", "Store",
                                       "Dentist", "MedicalBusiness", "ProfessionalService"]):
            data.business_name = data.business_name or item.get("name", "")
            data.business_type = data.business_type or ld_type

            addr = item.get("address", {})
            if isinstance(addr, dict) and not data.address:
                parts = [addr.get("streetAddress", ""), addr.get("addressLocality", ""),
                         addr.get("addressRegion", ""), addr.get("postalCode", "")]
                data.address = ", ".join(p for p in parts if p)

            phone = item.get("telephone", "")
            if phone and phone not in data.phones:
                data.phones.append(phone)

            email = item.get("email", "")
            if email and email not in data.emails:
                data.emails.append(email)

            hours = item.get("openingHoursSpecification", [])
            if hours and not data.hours:
                data.hours = hours if isinstance(hours, list) else [hours]

            desc = item.get("description", "")
            if desc and not data.description:
                data.description = desc

        # Services / products.
        if any(t in ld_type for t in ["Service", "Product", "Offer"]):
            name = item.get("name", "")
            if name and name not in data.services:
                data.services.append(name)

        # Team / people.
        if "Person" in ld_type:
            data.team_members.append({
                "name": item.get("name", ""),
                "title": item.get("jobTitle", ""),
                "email": item.get("email", ""),
            })

        # Reviews.
        if "Review" in ld_type:
            data.testimonials.append({
                "author": item.get("author", {}).get("name", "") if isinstance(item.get("author"), dict) else "",
                "text": item.get("reviewBody", "")[:500],
                "rating": item.get("reviewRating", {}).get("ratingValue", "") if isinstance(item.get("reviewRating"), dict) else "",
            })

        # FAQ.
        if "FAQPage" in ld_type:
            for entity in item.get("mainEntity", []):
                data.faq.append({
                    "question": entity.get("name", ""),
                    "answer": entity.get("acceptedAnswer", {}).get("text", "")[:500]
                              if isinstance(entity.get("acceptedAnswer"), dict) else "",
                })

        # Blog / article.
        if any(t in ld_type for t in ["BlogPosting", "Article", "NewsArticle"]):
            data.blog_posts.append({
                "title": item.get("headline", ""),
                "date": item.get("datePublished", ""),
                "url": item.get("url", ""),
            })

    # ------------------------------------------------------------------
    # Internal page discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _find_high_value_urls(data: SiteData) -> list[str]:
        base = data.url
        urls: list[str] = []
        for link in data._links:
            text = link.get("text", "")
            href = link.get("href", "")
            path = urlparse(href).path.strip("/").lower()
            segments = set(path.split("/"))

            if segments & HIGH_VALUE_SLUGS or any(s in text for s in HIGH_VALUE_SLUGS):
                full = urljoin(base, href)
                parsed = urlparse(full)
                base_domain = urlparse(base).netloc
                if parsed.netloc == base_domain and full not in urls and full != base:
                    urls.append(full)
        return urls

    # ------------------------------------------------------------------
    # Merge data from multiple pages
    # ------------------------------------------------------------------

    @staticmethod
    def _merge(primary: SiteData, secondary: SiteData) -> None:
        for email in secondary.emails:
            if email not in primary.emails:
                primary.emails.append(email)
        for phone in secondary.phones:
            if phone not in primary.phones and len(primary.phones) < 5:
                primary.phones.append(phone)
        if not primary.address and secondary.address:
            primary.address = secondary.address
        for k, v in secondary.social_links.items():
            primary.social_links.setdefault(k, v)
        primary.team_members.extend(secondary.team_members)
        primary.json_ld.extend(secondary.json_ld)
        for svc in secondary.services:
            if svc not in primary.services:
                primary.services.append(svc)
        primary.testimonials.extend(secondary.testimonials)
        primary.faq.extend(secondary.faq)
        primary.blog_posts.extend(secondary.blog_posts)
        for cta in secondary.cta_texts:
            if cta not in primary.cta_texts:
                primary.cta_texts.append(cta)
        for h in secondary.hiring:
            if h not in primary.hiring:
                primary.hiring.append(h)
        for sig in secondary.company_size_signals:
            if sig not in primary.company_size_signals:
                primary.company_size_signals.append(sig)
        for sig in secondary.pricing_signals:
            if sig not in primary.pricing_signals:
                primary.pricing_signals.append(sig)
        for logo in secondary.customer_logos:
            if logo not in primary.customer_logos:
                primary.customer_logos.append(logo)
        for tool in secondary.tools_mentioned:
            if tool not in primary.tools_mentioned:
                primary.tools_mentioned.append(tool)
        for heading in secondary.headings:
            if heading not in primary.headings:
                primary.headings.append(heading)
        primary._clean_texts.extend(secondary._clean_texts)

    # ------------------------------------------------------------------
    # Convert to leads
    # ------------------------------------------------------------------

    @staticmethod
    def _to_leads(data: SiteData) -> list[Lead]:
        raw = data.to_dict()

        if data.emails:
            return [
                Lead(
                    name=data.business_name or data.title,
                    email=email,
                    company=data.business_name or data.title,
                    phone=data.phones[0] if data.phones else None,
                    location=data.address,
                    website=data.url,
                    source=LeadSource.PLAYWRIGHT,
                    raw_data=raw,
                )
                for email in data.emails
            ]

        # Even without emails, rich business data is valuable.
        return [Lead(
            name=data.business_name or data.title,
            company=data.business_name or data.title,
            phone=data.phones[0] if data.phones else None,
            location=data.address,
            website=data.url,
            source=LeadSource.PLAYWRIGHT,
            raw_data=raw,
        )]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _attr(node: Any, name: str) -> str:
    return node.attrs.get(name) or ""


def _clean_emails(emails: list[str]) -> list[str]:
    seen: set[str] = set()
    clean: list[str] = []
    for email in emails:
        email = email.lower().strip()
        if email in seen:
            continue
        if any(skip in email for skip in SKIP_EMAILS):
            continue
        if not re.match(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", email):
            continue
        seen.add(email)
        clean.append(email)
    return clean


_TECH_PATTERNS: dict[str, list[str]] = {
    "React": ["react", "react-dom"],
    "Next.js": ["_next/"],
    "Vue": ["vue.js", "vue.min.js"],
    "Nuxt": ["_nuxt/"],
    "Angular": ["angular"],
    "jQuery": ["jquery"],
    "WordPress": ["wp-content", "wp-includes"],
    "Shopify": ["cdn.shopify"],
    "Wix": ["wix.com", "parastorage"],
    "Squarespace": ["squarespace"],
    "Webflow": ["webflow"],
    "Google Analytics": ["google-analytics", "gtag", "googletagmanager"],
    "Google Maps": ["maps.googleapis"],
    "HubSpot": ["hubspot"],
    "Intercom": ["intercom"],
    "Drift": ["drift"],
    "Hotjar": ["hotjar"],
    "Stripe": ["stripe.com/v3"],
    "Segment": ["segment.com/analytics"],
    "Salesforce": ["salesforce", "pardot"],
    "Mailchimp": ["mailchimp"],
    "Zendesk": ["zendesk"],
    "Freshdesk": ["freshdesk"],
    "Crisp": ["crisp.chat"],
    "LiveChat": ["livechatinc"],
}
