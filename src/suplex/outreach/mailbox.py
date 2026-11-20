from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import uuid
from pathlib import Path

from suplex.store.db import LeadStore

MAILBOX_BASE_DIR = Path.home() / ".config" / "suplex" / "mailboxes"
GWS_INSTALL_HINT = (
    "gws (Google Workspace CLI) not found. "
    "Install: npm install -g @googleworkspace/cli or brew install googleworkspace-cli"
)


def add_mailbox(
    email: str,
    display_name: str = "",
    daily_limit: int = 10,
    store: LeadStore | None = None,
) -> dict:
    store = store or LeadStore()
    if not shutil.which("gws"):
        return {"error": GWS_INSTALL_HINT}

    mailbox_id = uuid.uuid4().hex[:12]
    config_dir = MAILBOX_BASE_DIR / mailbox_id
    try:
        store.add_mailbox(mailbox_id, email, display_name or email, str(config_dir), daily_limit)
    except sqlite3.IntegrityError:
        return {"error": f"Mailbox {email} is already registered. See: suplex mailbox list"}
    config_dir.mkdir(parents=True, exist_ok=True)

    return {
        "mailbox_id": mailbox_id,
        "email": email,
        "config_dir": str(config_dir),
        "status": "created",
        "next_step": f"Run: GOOGLE_WORKSPACE_CLI_CONFIG_DIR={config_dir} gws auth login",
    }


def verify_mailbox(mailbox_id: str, store: LeadStore | None = None) -> dict:
    store = store or LeadStore()
    mailbox = store.get_mailbox(mailbox_id)
    if not mailbox:
        return {"error": f"Mailbox {mailbox_id} not found"}

    gws = shutil.which("gws")
    if not gws:
        return {"error": GWS_INSTALL_HINT}

    try:
        result = subprocess.run(
            [gws, "gmail", "users", "getProfile", "--params", '{"userId": "me"}'],
            capture_output=True, text=True, env=gws_env(mailbox), timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"mailbox_id": mailbox_id, "status": "error", "error": str(exc)}
    if result.returncode == 0:
        return {"mailbox_id": mailbox_id, "email": mailbox["email"], "status": "verified"}
    return {"mailbox_id": mailbox_id, "status": "auth_failed", "error": result.stderr[:200]}


def list_mailboxes(store: LeadStore | None = None) -> list[dict]:
    store = store or LeadStore()
    mailboxes = store.list_mailboxes()
    for mb in mailboxes:
        mb["sends_today"] = store.get_mailbox_sends_today(mb["id"])
    return mailboxes


def gws_env(mailbox: dict) -> dict:
    return {**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": mailbox["gws_config_dir"]}
