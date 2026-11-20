from __future__ import annotations

import pytest

from suplex.urls import domain_of, normalize_url, strip_www


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("acme.com", "https://acme.com"),
        ("www.acme.com/contact", "https://www.acme.com/contact"),
        ("//acme.com", "https://acme.com"),
        ("http://acme.com", "http://acme.com"),
        ("  https://acme.com  ", "https://acme.com"),
    ],
)
def test_normalize_url_adds_a_scheme_only_when_missing(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_normalize_url_rejects_empty_input() -> None:
    with pytest.raises(ValueError):
        normalize_url("   ")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("www.acme.com", "acme.com"),
        # The old code used str.lstrip("www."), which strips characters, not a prefix.
        ("web.com", "web.com"),
        ("wix.com", "wix.com"),
        ("www.wwwonderful.com", "wwwonderful.com"),
        ("WWW.Acme.com", "acme.com"),
    ],
)
def test_strip_www_removes_only_the_prefix(host: str, expected: str) -> None:
    assert strip_www(host) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.web.com/pricing", "web.com"),
        ("wix.com", "wix.com"),
        ("https://acme.com:8443/x", "acme.com"),
        ("localhost", None),
        ("", None),
    ],
)
def test_domain_of(url: str, expected: str | None) -> None:
    assert domain_of(url) == expected
