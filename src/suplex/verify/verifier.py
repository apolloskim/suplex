from __future__ import annotations

import asyncio
import logging
import platform
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
    def __init__(
        self,
        *,
        reacher_api_key: str | None = None,
        million_verifier_key: str | None = None,
    ) -> None:
        config = Config.load()
        self.reacher_api_key = reacher_api_key or config.reacher_api_key
        self.million_verifier_key = million_verifier_key or config.million_verifier_api_key
        self._local_url = (config.reacher_url or REACHER_LOCAL_URL).rstrip("/")
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

        if await self._ping(self._local_url):
            self._reacher_url = self._local_url
            self.setup_log.append("Email verification ready.")
            return True

        self.setup_log.append("Setting up email verification (SMTP handshake via local Docker)...")
        if await self._ensure_docker_and_reacher():
            self._reacher_url = REACHER_LOCAL_URL
            self.setup_log.append("Email verification ready — verifies emails by connecting to mail servers.")
            return True

        if self.reacher_api_key:
            self._reacher_url = REACHER_HOSTED_URL
            self._reacher_headers = {"Authorization": self.reacher_api_key}
            self.setup_log.append("Email verification ready (hosted API).")
            return True

        self.setup_log.append("Email verification limited to syntax checks — Docker not available for SMTP verification.")
        return False

    @staticmethod
    async def _ping(url: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.get(url)
                return resp.status_code in (200, 404)
        except (httpx.HTTPError, httpx.ConnectError):
            return False

    @staticmethod
    async def _ensure_docker_and_reacher() -> bool:
        runtime = await _find_or_install_runtime()
        if not runtime:
            logger.info("No container runtime found (Docker/OrbStack/Podman). Skipping SMTP verification.")
            return False

        if not await _is_daemon_running(runtime):
            started = await _start_daemon(runtime)
            if not started:
                logger.info("Could not start %s daemon. Skipping SMTP verification.", runtime)
                return False
            for _ in range(30):
                if await _is_daemon_running(runtime):
                    break
                await asyncio.sleep(1)
            else:
                logger.warning("%s daemon started but not responsive after 30s", runtime)
                return False

        cmd = runtime

        proc = await asyncio.create_subprocess_exec(
            cmd, "inspect", "-f", "{{.State.Running}}", REACHER_CONTAINER_NAME,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()

        if proc.returncode == 0 and b"true" in stdout:
            logger.info("Reacher already running")
            return True

        if proc.returncode == 0:
            await _run_cmd(cmd, "start", REACHER_CONTAINER_NAME)
        else:
            logger.info("Setting up email verification (one-time, ~30s)...")
            proc = await asyncio.create_subprocess_exec(
                cmd, "run", "-d",
                "--name", REACHER_CONTAINER_NAME,
                "-p", "8080:8080",
                "--restart", "unless-stopped",
                REACHER_DOCKER_IMAGE,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("Failed to start Reacher: %s", stderr.decode()[:200])
                return False

        for _ in range(20):
            if await EmailVerifier._ping(REACHER_LOCAL_URL):
                logger.info("Reacher ready at localhost:8080")
                return True
            await asyncio.sleep(1)

        logger.warning("Reacher container started but not responding")
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


async def _find_or_install_runtime() -> str | None:
    for cmd in ("docker", "podman"):
        if shutil.which(cmd):
            return cmd

    installed = await _install_docker()
    if installed:
        for cmd in ("docker", "podman"):
            if shutil.which(cmd):
                return cmd
    return None


async def _install_docker() -> bool:
    system = platform.system()

    if system == "Linux":
        logger.info("Installing Docker...")
        proc = await asyncio.create_subprocess_shell(
            "curl -fsSL https://get.docker.com | sh",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            logger.info("Docker installed successfully")
            return True
        logger.debug("Docker install failed: %s", stderr.decode()[:200])

    elif system == "Darwin":
        if not shutil.which("brew"):
            logger.info("Installing Homebrew...")
            proc = await asyncio.create_subprocess_shell(
                'NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.debug("Homebrew install failed: %s", stderr.decode()[:200])
                return False
            for brew_path in ("/opt/homebrew/bin", "/usr/local/bin"):
                import os
                if os.path.isfile(f"{brew_path}/brew"):
                    os.environ["PATH"] = f"{brew_path}:{os.environ['PATH']}"
                    break

        if shutil.which("brew"):
            logger.info("Installing Docker via Homebrew (one-time)...")
            proc = await asyncio.create_subprocess_exec(
                "brew", "install", "docker", "colima",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                proc = await asyncio.create_subprocess_exec(
                    "colima", "start",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                logger.info("Docker + colima installed and started")
                return True
            logger.debug("Homebrew install failed: %s", stderr.decode()[:200])

    return False


async def _is_daemon_running(runtime: str) -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime, "info",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
        return proc.returncode == 0
    except (asyncio.TimeoutError, OSError):
        return False


async def _start_daemon(runtime: str) -> bool:
    system = platform.system()

    if system == "Darwin":
        for app in ("Docker", "OrbStack"):
            try:
                proc = await asyncio.create_subprocess_exec(
                    "open", "-a", app, "-g",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
                if proc.returncode == 0:
                    logger.info("Starting %s...", app)
                    return True
            except OSError:
                continue

        if shutil.which("colima"):
            proc = await asyncio.create_subprocess_exec(
                "colima", "start",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0

    elif system == "Linux":
        if shutil.which("systemctl"):
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "start", "docker",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return proc.returncode == 0

        if runtime == "podman":
            return True

    return False


async def _run_cmd(*args: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()
