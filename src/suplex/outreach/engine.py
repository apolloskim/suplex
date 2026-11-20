"""Outreach engine — draft, send, reply detection, follow-ups.

Drafting is done by the agent (reads playbook + lead data, generates email).
This module handles the structured operations: send via gws, check replies,
manage follow-up queue, enforce rate limits and suppression.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from typing import Any

from suplex.outreach.mailbox import gws_env
from suplex.outreach.playbook import get_playbook, get_playbook_summary
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)


def preview_context(campaign_id: str, limit: int = 5, store: LeadStore | None = None) -> dict:
    """Get everything the agent needs to draft emails: playbook, leads, campaign info."""
    store = store or LeadStore()
    campaign = store.get_campaign(campaign_id)
    if not campaign:
        return {"error": f"Campaign {campaign_id} not found"}

    playbook = get_playbook()
    if not playbook:
        return {"error": "No playbook found. Create one first: suplex playbook init"}

    leads = store.get_eligible_leads(campaign_id, limit=limit)
    if not leads:
        return {"error": "No eligible leads for this campaign"}

    # Decode the JSON columns so the agent gets structured data, not strings.
    enriched_leads = []
    for lead in leads:
        lead_data = dict(lead)
        for column in ("raw_data", "enrichment_data"):
            value = lead_data.get(column)
            if isinstance(value, str):
                try:
                    lead_data[column] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        enriched_leads.append(lead_data)

    return {
        "campaign": campaign,
        "playbook": playbook,
        "playbook_summary": get_playbook_summary(),
        "leads": enriched_leads,
        "physical_address": campaign.get("physical_address", ""),
    }


def save_drafts(drafts: list[dict], campaign_id: str, mailbox_id: str,
                store: LeadStore | None = None) -> list[dict]:
    """Save agent-generated drafts to the outreach table.

    Each draft: {"lead_id": int, "subject": str, "body": str, "step": int}
    """
    store = store or LeadStore()
    saved = []
    for draft in drafts:
        outreach_id = uuid.uuid4().hex[:12]
        lead_id = draft["lead_id"]

        # Check suppression.
        lead_email = draft.get("email")
        if lead_email and store.is_suppressed(lead_email):
            logger.info("Skipping suppressed email: %s", lead_email)
            continue

        store.create_outreach(
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            mailbox_id=mailbox_id,
            step=draft.get("step", 1),
            subject=draft["subject"],
            body=draft["body"],
        )
        saved.append({"outreach_id": outreach_id, "lead_id": lead_id, "subject": draft["subject"]})

    return saved


def approve_drafts(outreach_ids: list[str], store: LeadStore | None = None) -> int:
    """Approve selected drafts for sending."""
    store = store or LeadStore()
    approved = 0
    for oid in outreach_ids:
        store.approve_outreach(oid)
        approved += 1
    return approved


def approve_all_drafts(campaign_id: str, store: LeadStore | None = None) -> int:
    """Approve all drafts in a campaign."""
    store = store or LeadStore()
    drafts = store.get_outreach_drafts(campaign_id, limit=1000)
    return approve_drafts([d["id"] for d in drafts], store=store)


def send_approved(campaign_id: str, limit: int = 10,
                  store: LeadStore | None = None) -> dict:
    """Send approved outreach emails via gws. Respects daily limits and suppression."""
    store = store or LeadStore()
    approved = store.get_approved_outreach(campaign_id, limit=limit)
    if not approved:
        return {"sent": 0, "errors": 0, "message": "No approved drafts to send"}

    gws_bin = shutil.which("gws")
    if not gws_bin:
        return {"error": "gws CLI not found"}

    sent = 0
    errors = 0
    skipped = 0

    for outreach in approved:
        mailbox_id = outreach["mailbox_id"]
        mailbox = store.get_mailbox(mailbox_id)
        if not mailbox:
            logger.error("Mailbox %s not found for outreach %s", mailbox_id, outreach["id"])
            errors += 1
            continue

        # Check daily limit.
        sends_today = store.get_mailbox_sends_today(mailbox_id)
        if sends_today >= mailbox["daily_limit"]:
            logger.warning("Daily limit reached for %s (%d/%d)", mailbox["email"], sends_today, mailbox["daily_limit"])
            skipped += 1
            continue

        # Check suppression.
        if store.is_suppressed(outreach["lead_email"]):
            logger.info("Suppressed: %s", outreach["lead_email"])
            skipped += 1
            continue

        # Send via gws.
        result = _send_via_gws(
            gws_bin=gws_bin,
            mailbox=mailbox,
            to=outreach["lead_email"],
            subject=outreach["subject"],
            body=outreach["body"],
            thread_id=outreach.get("gmail_thread_id"),  # For follow-ups in same thread.
        )

        if result.get("error"):
            store.mark_outreach_failed(outreach["id"], result["error"])
            errors += 1
        else:
            store.mark_outreach_sent(
                outreach["id"],
                gmail_thread_id=result.get("threadId", ""),
                gmail_message_id=result.get("id", ""),
            )
            sent += 1

    return {"sent": sent, "errors": errors, "skipped": skipped}


def check_replies(mailbox_id: str, store: LeadStore | None = None) -> dict:
    """Check for replies to sent outreach emails via gws."""
    store = store or LeadStore()
    mailbox = store.get_mailbox(mailbox_id)
    if not mailbox:
        return {"error": f"Mailbox {mailbox_id} not found"}

    gws_bin = shutil.which("gws")
    if not gws_bin:
        return {"error": "gws CLI not found"}

    sent_threads = store.get_sent_threads(mailbox_id)
    if not sent_threads:
        return {"replies": 0, "checked": 0}

    env = gws_env(mailbox)
    replies_found = 0

    for thread_info in sent_threads:
        thread_id = thread_info["gmail_thread_id"]
        if not thread_id:
            continue

        # Get thread messages.
        try:
            result = subprocess.run(
                [gws_bin, "gmail", "users.threads", "get", "--userId", "me", "--id", thread_id,
                 "--format", "json"],
                capture_output=True, text=True, env=env, timeout=15,
            )
            if result.returncode != 0:
                continue

            thread_data = json.loads(result.stdout)
            messages = thread_data.get("messages", [])

            # Check if there's a reply (message not from us).
            for msg in messages:
                msg_id = msg.get("id", "")
                if msg_id == thread_info["gmail_message_id"]:
                    continue  # Our sent message.
                # This is a reply.
                store.mark_outreach_replied(thread_info["id"])
                replies_found += 1

                # Check for opt-out in reply.
                snippet = msg.get("snippet", "").lower()
                _check_opt_out(snippet, thread_info["lead_email"], store)
                break

        except Exception as e:
            logger.warning("Error checking thread %s: %s", thread_id, e)

    return {"replies": replies_found, "checked": len(sent_threads)}


def get_due_followups(campaign_id: str, store: LeadStore | None = None) -> list[dict]:
    """Get leads due for follow-up in a campaign."""
    store = store or LeadStore()
    return store.get_outreach_due_followups(campaign_id)


def _send_via_gws(
    gws_bin: str,
    mailbox: dict,
    to: str,
    subject: str,
    body: str,
    thread_id: str | None = None,
) -> dict[str, Any]:
    """Send an email via gws gmail +send."""
    env = gws_env(mailbox)

    cmd = [gws_bin, "gmail", "+send",
           "--to", to,
           "--subject", subject,
           "--body", body]

    if thread_id:
        cmd.extend(["--threadId", thread_id])

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, env=env, timeout=30,
        )
        if result.returncode != 0:
            return {"error": result.stderr[:300]}

        # Parse gws JSON output for message/thread IDs.
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"id": "", "threadId": ""}

    except subprocess.TimeoutExpired:
        return {"error": "gws send timed out"}
    except Exception as e:
        return {"error": str(e)}


def _check_opt_out(snippet: str, email: str, store: LeadStore) -> None:
    """Check if a reply contains opt-out signals and suppress if so."""
    opt_out_signals = [
        "stop", "unsubscribe", "opt out", "remove me", "not interested",
        "no thanks", "no thank you", "don't email", "dont email",
        "don't contact", "dont contact", "take me off", "leave me alone",
    ]
    snippet_lower = snippet.lower()
    for signal in opt_out_signals:
        if signal in snippet_lower:
            store.add_suppression(email, reason="opt_out_reply", source="auto_detected")
            logger.info("Auto-suppressed %s (opt-out detected in reply)", email)
            return
