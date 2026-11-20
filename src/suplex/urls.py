"""Small URL helpers shared by the scrapers and the enrichment pipeline."""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    """Return ``url`` with a scheme, defaulting to https.

    Lead websites arrive in many shapes ("acme.com", "www.acme.com/contact",
    "//acme.com"). httpx and Playwright both reject anything without a scheme.
    """
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")
    if url.startswith("//"):
        return f"https:{url}"
    if "://" not in url:
        return f"https://{url}"
    return url


def strip_www(host: str) -> str:
    """Drop one leading ``www.`` label.

    Deliberately not ``host.lstrip("www.")``: lstrip removes a *set of
    characters*, so it turns "web.com" into "eb.com" and "wix.com" into "ix.com".
    """
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def domain_of(url: str) -> str | None:
    """Registrable-looking host for a URL ("https://www.acme.com/x" -> "acme.com")."""
    try:
        parsed = urlparse(normalize_url(url))
    except ValueError:
        return None
    host = strip_www(parsed.hostname or "")
    return host if "." in host else None
