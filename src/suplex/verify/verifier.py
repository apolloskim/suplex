from __future__ import annotations

import logging
from typing import Any

import httpx
from email_validator import EmailNotValidError, validate_email

from suplex.config import Config
from suplex.models import VerificationResult, VerificationStatus
from suplex.verify.reacher_setup import (
    CHOICE_MANAGED,
    REACHER_LOCAL_URL,
    UNAVAILABLE_MESSAGE,
    local_url,
    ping,
    set_up_local_reacher,
)

logger = logging.getLogger(__name__)

REACHER_HOSTED_URL = "https://api.reacher.email"


class EmailVerifier:
    def __init__(
        self,
        *,
        reacher_api_key: str | None = None,
        million_verifier_key: str | None = None,
    ) -> None:
        config = Config.load()
        self.reacher_api_key = reacher_api_key or config.reacher_api_key
        self.million_verifier_key = million_verifier_key or config.million_verifier_api_key
        self._local_url = local_url(config)
        self._setup_choice = config.verification_setup
        self._reacher_url: str | None = None
        self._reacher_headers: dict[str, str] = {}
        self._setup_attempted = False
        self.setup_log: list[str] = []

    async def _ensure_reacher(self) -> bool:
        if self._reacher_url:
            return True

        if self._setup_attempted:
            return False
        self._setup_attempted = True

        if await ping(self._local_url):
            self._reacher_url = self._local_url
            self.setup_log.append("Email verification ready.")
            return True

        if self._setup_choice == CHOICE_MANAGED:
            self.setup_log.append("Starting local email verification (Reacher in Docker)...")
            if await set_up_local_reacher():
                self._reacher_url = REACHER_LOCAL_URL
                self.setup_log.append("Email verification ready.")
                return True

        if self.reacher_api_key:
            self._reacher_url = REACHER_HOSTED_URL
            self._reacher_headers = {"Authorization": self.reacher_api_key}
            self.setup_log.append("Email verification ready (hosted API).")
            return True

        self.setup_log.append(UNAVAILABLE_MESSAGE)
        logger.warning(UNAVAILABLE_MESSAGE)
        return False

    async def verify(self, email: str) -> VerificationResult:
        syntax = self._check_syntax(email)
        if syntax.status == VerificationStatus.INVALID:
            return syntax

        has_reacher = await self._ensure_reacher()
        if has_reacher:
            reacher_result = await self._check_reacher(email)
            if reacher_result.status == VerificationStatus.INVALID:
                return reacher_result

            if reacher_result.is_catch_all and self.million_verifier_key:
                return await self._check_million_verifier(email)

            return reacher_result

        return VerificationResult(
            email=email,
            status=VerificationStatus.UNKNOWN,
            reason="no_reacher — syntax valid but SMTP unchecked",
        )

    @staticmethod
    def _check_syntax(email: str) -> VerificationResult:
        try:
            result = validate_email(email, check_deliverability=False)
            return VerificationResult(
                email=result.normalized,
                status=VerificationStatus.PENDING,
                reason="syntax_ok",
            )
        except EmailNotValidError as e:
            return VerificationResult(
                email=email,
                status=VerificationStatus.INVALID,
                reason=f"syntax_error: {e}",
            )

    async def _check_reacher(self, email: str) -> VerificationResult:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    f"{self._reacher_url}/v0/check_email",
                    headers=self._reacher_headers,
                    json={"to_email": email},
                )
                resp.raise_for_status()
                return self._parse_reacher(email, resp.json())
        except httpx.HTTPError as e:
            logger.warning("Reacher failed for %s: %s", email, e)
            return VerificationResult(
                email=email, status=VerificationStatus.UNKNOWN, reason=f"reacher_error: {e}",
            )

    @staticmethod
    def _parse_reacher(email: str, data: dict[str, Any]) -> VerificationResult:
        is_reachable = data.get("is_reachable", "unknown")
        mx = data.get("mx", {})
        smtp = data.get("smtp", {})
        misc = data.get("misc", {})

        mx_host = mx.get("records", [None])[0] if mx.get("records") else None
        is_catch_all = misc.get("is_catch_all", False)

        if is_reachable == "safe":
            status, reason = VerificationStatus.VALID, "reachable"
        elif is_reachable == "invalid":
            status = VerificationStatus.INVALID
            reason = smtp.get("error", {}).get("type", "smtp_rejected")
        elif is_reachable == "risky":
            status = VerificationStatus.CATCH_ALL if is_catch_all else VerificationStatus.VALID
            reason = "catch_all" if is_catch_all else "risky_but_deliverable"
        else:
            status, reason = VerificationStatus.UNKNOWN, "unknown_reachability"

        return VerificationResult(
            email=email, status=status, reason=reason, mx_host=mx_host,
            is_catch_all=is_catch_all, is_disposable=misc.get("is_disposable", False),
        )

    async def _check_million_verifier(self, email: str) -> VerificationResult:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.get(
                    "https://api.millionverifier.com/api/v3/",
                    params={"api": self.million_verifier_key, "email": email},
                )
                resp.raise_for_status()
                data = resp.json()

            mv_result = data.get("result", "unknown")
            status_map = {
                "ok": VerificationStatus.VALID,
                "catch_all": VerificationStatus.CATCH_ALL,
                "invalid": VerificationStatus.INVALID,
                "disposable": VerificationStatus.INVALID,
                "unknown": VerificationStatus.UNKNOWN,
            }
            return VerificationResult(
                email=email,
                status=status_map.get(mv_result, VerificationStatus.UNKNOWN),
                reason=f"million_verifier: {mv_result}",
                is_catch_all=mv_result == "catch_all",
                is_disposable=mv_result == "disposable",
            )
        except httpx.HTTPError as e:
            return VerificationResult(
                email=email, status=VerificationStatus.CATCH_ALL,
                reason=f"million_verifier_error: {e}", is_catch_all=True,
            )
