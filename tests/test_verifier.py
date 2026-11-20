"""Verifier tests. Reacher and Docker are never contacted: the HTTP layer is stubbed."""

from __future__ import annotations

from suplex.models import VerificationStatus
from suplex.verify import verifier as verifier_module
from suplex.verify.verifier import EmailVerifier


def test_parse_reacher_maps_reachability_to_status() -> None:
    parse = EmailVerifier._parse_reacher
    assert parse("a@x.com", {"is_reachable": "safe"}).status == VerificationStatus.VALID
    assert parse("a@x.com", {"is_reachable": "invalid", "smtp": {}}).status == VerificationStatus.INVALID
    risky = parse("a@x.com", {"is_reachable": "risky", "misc": {"is_catch_all": True}})
    assert risky.status == VerificationStatus.CATCH_ALL and risky.is_catch_all
    assert parse("a@x.com", {}).status == VerificationStatus.UNKNOWN


async def test_invalid_syntax_is_rejected_without_any_network_call(monkeypatch) -> None:
    async def fail(self):
        raise AssertionError("Reacher must not be set up for a syntactically invalid address")

    monkeypatch.setattr(EmailVerifier, "_ensure_reacher", fail)

    result = await EmailVerifier().verify("not-an-email")

    assert result.status == VerificationStatus.INVALID


async def test_reacher_url_setting_is_used(monkeypatch) -> None:
    # REACHER_URL was read into the config but then ignored in favour of localhost:8080.
    monkeypatch.setenv("REACHER_URL", "http://reacher.internal:9000/")
    pinged: list[str] = []

    async def fake_ping(url: str) -> bool:
        pinged.append(url)
        return True

    monkeypatch.setattr(verifier_module.EmailVerifier, "_ping", staticmethod(fake_ping))
    v = EmailVerifier()

    assert await v._ensure_reacher() is True
    assert pinged == ["http://reacher.internal:9000"]
    assert v._reacher_url == "http://reacher.internal:9000"
