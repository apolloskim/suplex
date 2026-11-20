from __future__ import annotations

import asyncio
import logging
import platform
import shutil
from dataclasses import dataclass, field

import httpx

from suplex.config import Config

logger = logging.getLogger(__name__)

REACHER_DOCKER_IMAGE = "reacherhq/backend:latest"
REACHER_CONTAINER_NAME = "suplex-reacher"
REACHER_PORT = 8080
REACHER_LOCAL_URL = f"http://localhost:{REACHER_PORT}"
DOCKER_DOCS_URL = "https://docs.docker.com/get-docker/"
INSTALL_TIMEOUT = 900
CONTAINER_START_TIMEOUT = 300

CHOICE_MANAGED = "managed"
CHOICE_MANUAL = "manual"
CHOICE_HOSTED = "hosted"
CHOICE_SKIP = "skip"

CHOICE_LABELS = {
    "": "not decided yet",
    CHOICE_MANAGED: "Suplex manages a local Reacher container",
    CHOICE_MANUAL: "you will install Docker yourself",
    CHOICE_HOSTED: "hosted Reacher key",
    CHOICE_SKIP: "skipped",
}

UNAVAILABLE_MESSAGE = (
    "Email verification is off: it needs a local Reacher service in Docker or a hosted Reacher key, "
    "and neither is set up, so emails are only syntax-checked. To turn it on, run `suplex doctor` "
    "in a terminal. Agents: ask the user first, then call ensure_reacher with user_approved=true."
)


@dataclass(frozen=True)
class SetupPlan:
    runtime: str | None
    commands: list[str] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return bool(self.commands)

    @property
    def installs_software(self) -> bool:
        return self.available and self.runtime is None


def local_url(config: Config) -> str:
    return (config.reacher_url or REACHER_LOCAL_URL).rstrip("/")


def find_runtime() -> str | None:
    for command in ("docker", "podman"):
        if shutil.which(command):
            return command
    return None


def plan_local_setup() -> SetupPlan:
    runtime = find_runtime()
    run_container = (
        f"{runtime or 'docker'} run -d --name {REACHER_CONTAINER_NAME} -p {REACHER_PORT}:8080 "
        f"--restart unless-stopped {REACHER_DOCKER_IMAGE}"
    )
    if runtime:
        return SetupPlan(runtime, [run_container])
    if platform.system() == "Darwin" and shutil.which("brew"):
        return SetupPlan(None, ["brew install docker colima", "colima start", run_container])
    return SetupPlan(None)


def needs_decision(config: Config) -> bool:
    choice = config.verification_setup
    if choice == "":
        return True
    return choice == CHOICE_MANUAL and find_runtime() is not None


async def ping(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(f"{url}/version")
        return resp.status_code == 200 and "version" in resp.json()
    except (httpx.HTTPError, ValueError, TypeError):
        return False


async def local_port_taken() -> bool:
    if await ping(REACHER_LOCAL_URL):
        return False
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", REACHER_PORT), timeout=2)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    return True


async def set_up_local_reacher() -> bool:
    plan = plan_local_setup()
    if not plan.available:
        return False
    if await local_port_taken():
        logger.warning("Port %d is used by another program, so a local Reacher cannot start", REACHER_PORT)
        return False

    runtime = plan.runtime
    if runtime is None:
        if not await _install_with_homebrew():
            return False
        runtime = find_runtime()
        if runtime is None:
            return False

    if not await _is_daemon_running(runtime):
        if not await _start_daemon(runtime):
            logger.warning("Could not start the %s daemon", runtime)
            return False
        for _ in range(30):
            if await _is_daemon_running(runtime):
                break
            await asyncio.sleep(1)
        else:
            logger.warning("%s daemon started but not responsive after 30s", runtime)
            return False

    if not await _start_container(runtime):
        return False

    for _ in range(20):
        if await ping(REACHER_LOCAL_URL):
            logger.info("Reacher ready at %s", REACHER_LOCAL_URL)
            return True
        await asyncio.sleep(1)

    logger.warning("Reacher container started but not responding")
    return False


async def _start_container(runtime: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        runtime, "inspect", "-f", "{{.State.Running}}", REACHER_CONTAINER_NAME,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode == 0 and b"true" in stdout:
        return True
    if proc.returncode == 0:
        return await _run_quiet(runtime, "start", REACHER_CONTAINER_NAME) == 0

    code, stderr = await _run_capture(
        runtime, "run", "-d",
        "--name", REACHER_CONTAINER_NAME,
        "-p", f"{REACHER_PORT}:8080",
        "--restart", "unless-stopped",
        REACHER_DOCKER_IMAGE,
        timeout=CONTAINER_START_TIMEOUT,
    )
    if code != 0:
        logger.warning("Failed to start Reacher: %s", stderr[:200])
        return False
    return True


async def _install_with_homebrew() -> bool:
    code, stderr = await _run_capture("brew", "install", "docker", "colima", timeout=INSTALL_TIMEOUT)
    if code != 0:
        logger.warning("brew install docker colima failed: %s", stderr[:200])
        return False
    return await _run_quiet("colima", "start") == 0


async def _run_capture(*args: str, timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, f"{args[0]} {args[1]} did not finish within {int(timeout)}s"
    return proc.returncode or 0, stderr.decode(errors="replace")


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
                if await _run_quiet("open", "-a", app, "-g") == 0:
                    return True
            except OSError:
                continue
        if shutil.which("colima"):
            return await _run_quiet("colima", "start") == 0

    elif system == "Linux":
        if runtime == "podman":
            return True
        if shutil.which("systemctl"):
            return await _run_quiet("systemctl", "start", "docker") == 0

    return False


async def _run_quiet(*args: str) -> int:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    return await proc.wait()
