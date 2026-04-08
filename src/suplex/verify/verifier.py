"""Email verification — layered pipeline: syntax → Reacher (Docker or hosted API) → MillionVerifier."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

import httpx
from email_validator import EmailNotValidError, validate_email

from suplex.config import Config
from suplex.models import VerificationResult, VerificationStatus

logger = logging.getLogger(__name__)

REACHER_DOCKER_IMAGE = "reacherhq/backend:latest"
REACHER_CONTAINER_NAME = "suplex-reacher"
REACHER_LOCAL_URL = "http://localhost:8080"
REACHER_HOSTED_URL = "https://api.reacher.email"


class EmailVerifier:
    """Multi-layer email verification pipeline.

    Reacher modes (in priority order):
    1. Docker (free, self-hosted) — auto-started if Docker is available
    2. Hosted API (paid) — if reacher_api_key is set
    3. Syntax + DNS only (degraded) — if neither is available
    """

    def __init__(
        self,
        *,
        reacher_api_key: str | None = None,
        million_verifier_key: str | None = None,
    ) -> None:
        config = Config.load()
        self.reacher_api_key = reacher_api_key or config.reacher_api_key
        self.million_verifier_key = million_verifier_key or config.million_verifier_api_key
        self._reacher_url: str | None = None
        self._reacher_headers: dict[str, str] = {}
        self._docker_started = False

    async def _ensure_reacher(self) -> bool:
        """Ensure Reacher is available. Try Docker first, then hosted API."""
        if self._reacher_url:
            return True

        # Try 1: Is Reacher already running locally?
        if await self._ping_reacher(REACHER_LOCAL_URL):
            self._reacher_url = REACHER_LOCAL_URL
            logger.info("Reacher found at %s", REACHER_LOCAL_URL)
            return True

        # Try 2: Start Docker container.
        if await self._start_docker():
            self._reacher_url = REACHER_LOCAL_URL
            self._docker_started = True
            logger.info("Started Reacher Docker container")
            return True

        # Try 3: Use hosted API if key is available.
        if self.reacher_api_key:
            self._reacher_url = REACHER_HOSTED_URL
            self._reacher_headers = {"Authorization": self.reacher_api_key}
            logger.info("Using Reacher hosted API")
            return True

        logger.warning("No Reacher available — verification will be syntax + DNS only")
        return False

    @staticmethod
    async def _ping_reacher(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(url)
                return resp.status_code in (200, 404)  # 404 = running, no root route
        except (httpx.HTTPError, httpx.ConnectError):
            return False

    @staticmethod
    async def _start_docker() -> bool:
        """Start Reacher in Docker if Docker is available."""
        if not shutil.which("docker"):
            logger.debug("Docker not found on PATH")
            return False

        try:
            # Check if container already exists (stopped).
            proc = await asyncio.create_subprocess_exec(
                "docker", "inspect", REACHER_CONTAINER_NAME,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

            if proc.returncode == 0:
                # Container exists — start it.
                proc = await asyncio.create_subprocess_exec(
                    "docker", "start", REACHER_CONTAINER_NAME,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            else:
                # Pull + create container.
                proc = await asyncio.create_subprocess_exec(
                    "docker", "run", "-d",
                    "--name", REACHER_CONTAINER_NAME,
                    "-p", "8080:8080",
                    "--restart", "unless-stopped",
                    REACHER_DOCKER_IMAGE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await proc.communicate()
                if proc.returncode != 0:
                    logger.warning("Docker run failed: %s", stderr.decode())
                    return False

            # Wait for it to be ready.
            for _ in range(15):
                if await EmailVerifier._ping_reacher(REACHER_LOCAL_URL):
                    return True
                await asyncio.sleep(1)

            logger.warning("Reacher container started but not responding")
            return False

        except Exception as e:
            logger.debug("Docker startup failed: %s", e)
            return False

    async def verify(self, email: str) -> VerificationResult:
        """Run the full verification pipeline for a single email."""
        # Layer 1: Syntax check.
        syntax = self._check_syntax(email)
        if syntax.status == VerificationStatus.INVALID:
            return syntax

        # Layer 2: Reacher (DNS + MX + SMTP).
        has_reacher = await self._ensure_reacher()
        if has_reacher:
            reacher_result = await self._check_reacher(email)
            if reacher_result.status == VerificationStatus.INVALID:
                return reacher_result

            # Layer 3: If catch-all detected and we have MillionVerifier, double-check.
            if reacher_result.is_catch_all and self.million_verifier_key:
                return await self._check_million_verifier(email)

            return reacher_result

        # Degraded mode: syntax passed, but we can't verify further.
        return VerificationResult(
            email=email,
            status=VerificationStatus.UNKNOWN,
            reason="no_reacher_available — syntax valid but SMTP unchecked",
        )

    async def verify_batch(self, emails: list[str]) -> list[VerificationResult]:
        """Verify a batch of emails."""
        results: list[VerificationResult] = []
        for email in emails:
            result = await self.verify(email)
            results.append(result)
        return results

    @staticmethod
    def _check_syntax(email: str) -> VerificationResult:
        try:
            result = validate_email(email, check_deliverability=False)
            return VerificationResult(
                email=result.normalized,
                status=VerificationStatus.PENDING,  # Pass — continue pipeline.
                reason="syntax_ok",
            )
        except EmailNotValidError as e:
            return VerificationResult(
                email=email,
                status=VerificationStatus.INVALID,
                reason=f"syntax_error: {e}",
            )

    async def _check_reacher(self, email: str) -> VerificationResult:
        """Call Reacher's /v0/check_email endpoint (Docker or hosted)."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self._reacher_url}/v0/check_email",
                    headers=self._reacher_headers,
                    json={"to_email": email},
                )
                resp.raise_for_status()
                data = resp.json()
                return self._parse_reacher_response(email, data)
        except httpx.HTTPError as e:
            logger.warning("Reacher failed for %s: %s", email, e)
            return VerificationResult(
                email=email,
                status=VerificationStatus.UNKNOWN,
                reason=f"reacher_error: {e}",
            )

    @staticmethod
    def _parse_reacher_response(email: str, data: dict[str, Any]) -> VerificationResult:
        is_reachable = data.get("is_reachable", "unknown")
        mx = data.get("mx", {})
        smtp = data.get("smtp", {})
        misc = data.get("misc", {})

        mx_host = mx.get("records", [None])[0] if mx.get("records") else None
        is_catch_all = misc.get("is_catch_all", False)

        if is_reachable == "safe":
            status = VerificationStatus.VALID
            reason = "reachable"
        elif is_reachable == "invalid":
            status = VerificationStatus.INVALID
            reason = smtp.get("error", {}).get("type", "smtp_rejected")
        elif is_reachable == "risky":
            status = VerificationStatus.CATCH_ALL if is_catch_all else VerificationStatus.VALID
            reason = "catch_all" if is_catch_all else "risky_but_deliverable"
        else:
            status = VerificationStatus.UNKNOWN
            reason = "unknown_reachability"

        return VerificationResult(
            email=email,
            status=status,
            reason=reason,
            mx_host=mx_host,
            is_catch_all=is_catch_all,
            is_disposable=misc.get("is_disposable", False),
        )

    async def _check_million_verifier(self, email: str) -> VerificationResult:
        """Double-check catch-all emails via MillionVerifier API."""
        try:
            async with httpx.AsyncClient(timeout=30) as client:
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
            logger.warning("MillionVerifier failed for %s: %s", email, e)
            return VerificationResult(
                email=email,
                status=VerificationStatus.CATCH_ALL,
                reason=f"million_verifier_error: {e}",
                is_catch_all=True,
            )
