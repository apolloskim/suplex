from __future__ import annotations

from urllib.parse import urlparse


def normalize_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ValueError("URL is empty")
    if url.startswith("//"):
        return f"https:{url}"
    if "://" not in url:
        return f"https://{url}"
    return url


def strip_www(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def domain_of(url: str) -> str | None:
    try:
        parsed = urlparse(normalize_url(url))
    except ValueError:
        return None
    host = strip_www(parsed.hostname or "")
    return host if "." in host else None
