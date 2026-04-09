"""Mailbox management — isolated gws config dirs per Gmail account.

Each mailbox gets its own GOOGLE_WORKSPACE_CLI_CONFIG_DIR so credentials
don't bleed across accounts. Suplex manages switching by setting the env
var per gws invocation.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

from suplex.store.db import LeadStore

MAILBOX_BASE_DIR = Path.home() / ".config" / "suplex" / "mailboxes"


def add_mailbox(
    email: str,
    display_name: str = "",
    daily_limit: int = 10,
    store: LeadStore | None = None,
) -> dict:
    """Register a new mailbox and launch gws auth for it."""
    store = store or LeadStore()
    mailbox_id = uuid.uuid4().hex[:12]
    config_dir = MAILBOX_BASE_DIR / mailbox_id
    config_dir.mkdir(parents=True, exist_ok=True)

    # Check gws is installed.
    gws = shutil.which("gws")
    if not gws:
        return {"error": "gws CLI not found. Install: npm install -g @anthropic/gws or brew install gws"}

    store.add_mailbox(mailbox_id, email, display_name or email, str(config_dir), daily_limit)

    return {
        "mailbox_id": mailbox_id,
        "email": email,
        "config_dir": str(config_dir),
        "status": "created",
        "next_step": f"Run: GOOGLE_WORKSPACE_CLI_CONFIG_DIR={config_dir} gws auth login",
    }


def verify_mailbox(mailbox_id: str, store: LeadStore | None = None) -> dict:
    """Verify a mailbox can send by checking gws auth status."""
    store = store or LeadStore()
    mailbox = store.get_mailbox(mailbox_id)
    if not mailbox:
        return {"error": f"Mailbox {mailbox_id} not found"}

    gws = shutil.which("gws")
    if not gws:
        return {"error": "gws CLI not found"}

    env = {**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": mailbox["gws_config_dir"]}
    try:
        result = subprocess.run(
            [gws, "gmail", "users", "getProfile", "--userId", "me"],
            capture_output=True, text=True, env=env, timeout=15,
        )
        if result.returncode == 0:
            return {"mailbox_id": mailbox_id, "email": mailbox["email"], "status": "verified"}
        return {"mailbox_id": mailbox_id, "status": "auth_failed", "error": result.stderr[:200]}
    except Exception as e:
        return {"mailbox_id": mailbox_id, "status": "error", "error": str(e)}


def list_mailboxes(store: LeadStore | None = None) -> list[dict]:
    store = store or LeadStore()
    mailboxes = store.list_mailboxes()
    for mb in mailboxes:
        mb["sends_today"] = store.get_mailbox_sends_today(mb["id"])
    return mailboxes


def gws_env(mailbox: dict) -> dict:
    """Build env dict for a gws invocation with this mailbox's credentials."""
    return {**os.environ, "GOOGLE_WORKSPACE_CLI_CONFIG_DIR": mailbox["gws_config_dir"]}
