from __future__ import annotations

import json
import logging
import shutil
import subprocess
import uuid
from typing import Any

from suplex.outreach.mailbox import GWS_INSTALL_HINT, gws_env
from suplex.outreach.playbook import get_playbook, get_playbook_summary
from suplex.store.db import LeadStore

logger = logging.getLogger(__name__)

OPT_OUT_SIGNALS = (
    "stop", "unsubscribe", "opt out", "remove me", "not interested",
    "no thanks", "no thank you", "don't email", "dont email",
    "don't contact", "dont contact", "take me off", "leave me alone",
)


def preview_context(campaign_id: str, limit: int = 5, store: LeadStore | None = None) -> dict:
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

    decoded_leads = []
    for lead in leads:
        lead_data = dict(lead)
        for column in ("raw_data", "enrichment_data"):
            value = lead_data.get(column)
            if isinstance(value, str):
                try:
                    lead_data[column] = json.loads(value)
                except json.JSONDecodeError:
                    pass
        decoded_leads.append(lead_data)

    return {
        "campaign": campaign,
        "playbook": playbook,
        "playbook_summary": get_playbook_summary(),
        "leads": decoded_leads,
        "physical_address": campaign.get("physical_address", ""),
    }


def save_drafts(
    drafts: list[dict], campaign_id: str, mailbox_id: str, store: LeadStore | None = None,
) -> list[dict]:
    store = store or LeadStore()
    if not store.get_campaign(campaign_id):
        return [{"skipped": f"Campaign {campaign_id} not found"}]
    if not store.get_mailbox(mailbox_id):
        return [{"skipped": f"Mailbox {mailbox_id} not found"}]

    results = []
    for draft in drafts:
        lead_id = draft["lead_id"]
        step = draft.get("step", 1)
        skipped = _draft_skip_reason(store, campaign_id, lead_id, step)
        if skipped:
            logger.info("Draft for lead %s skipped: %s", lead_id, skipped)
            results.append({"lead_id": lead_id, "skipped": skipped})
            continue

        outreach_id = uuid.uuid4().hex[:12]
        store.create_outreach(
            outreach_id=outreach_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            mailbox_id=mailbox_id,
            step=step,
            subject=draft["subject"],
            body=draft["body"],
        )
        results.append({"outreach_id": outreach_id, "lead_id": lead_id, "subject": draft["subject"]})

    return results


def _draft_skip_reason(store: LeadStore, campaign_id: str, lead_id: int, step: int) -> str | None:
    lead = store.get_lead(lead_id)
    if lead is None:
        return "lead not found"
    if not lead.email:
        return "lead has no email"
    if store.is_suppressed(lead.email):
        return "email is on the suppression list"
    if store.outreach_exists(campaign_id, lead_id, step):
        return f"step {step} already exists for this lead in this campaign"
    return None


def approve_drafts(outreach_ids: list[str], store: LeadStore | None = None) -> int:
    store = store or LeadStore()
    return sum(1 for outreach_id in outreach_ids if store.approve_outreach(outreach_id))


def approve_all_drafts(campaign_id: str, store: LeadStore | None = None) -> int:
    store = store or LeadStore()
    drafts = store.get_outreach_drafts(campaign_id, limit=1000)
    return approve_drafts([d["id"] for d in drafts], store=store)


def send_approved(campaign_id: str, limit: int = 10, store: LeadStore | None = None) -> dict:
    store = store or LeadStore()
    approved = store.get_approved_outreach(campaign_id, limit=limit)
    if not approved:
        return {"sent": 0, "errors": 0, "message": "No approved drafts to send"}

    gws_bin = shutil.which("gws")
    if not gws_bin:
        return {"error": GWS_INSTALL_HINT}

    sent = 0
    errors = 0
    skipped = 0

    for outreach in approved:
        mailbox_id = outreach["mailbox_id"]
        mailbox = store.get_mailbox(mailbox_id)
        if not mailbox:
            logger.error("Mailbox %s not found for outreach %s", mailbox_id, outreach["id"])
            store.mark_outreach_failed(outreach["id"], f"Mailbox {mailbox_id} not found")
            errors += 1
            continue

        sends_today = store.get_mailbox_sends_today(mailbox_id)
        if sends_today >= mailbox["daily_limit"]:
            logger.warning("Daily limit reached for %s (%d/%d)", mailbox["email"], sends_today, mailbox["daily_limit"])
            skipped += 1
            continue

        if store.is_suppressed(outreach["lead_email"]):
            logger.info("Suppressed: %s", outreach["lead_email"])
            skipped += 1
            continue

        result = _send_via_gws(
            gws_bin=gws_bin,
            mailbox=mailbox,
            to=outreach["lead_email"],
            subject=outreach["subject"],
            body=outreach["body"],
            reply_to_message_id=store.get_previous_sent_message_id(
                campaign_id, outreach["lead_id"], outreach["step"],
            ),
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
    store = store or LeadStore()
    mailbox = store.get_mailbox(mailbox_id)
    if not mailbox:
        return {"error": f"Mailbox {mailbox_id} not found"}

    gws_bin = shutil.which("gws")
    if not gws_bin:
        return {"error": GWS_INSTALL_HINT}

    sent_threads = store.get_sent_threads(mailbox_id)
    if not sent_threads:
        return {"replies": 0, "checked": 0}

    env = gws_env(mailbox)
    replies_found = 0
    errors = 0

    for thread_info in sent_threads:
        thread_id = thread_info["gmail_thread_id"]
        messages = _fetch_thread_messages(gws_bin, env, thread_id)
        if messages is None:
            errors += 1
            continue

        reply = _first_reply(messages, our_message_ids=store.get_thread_message_ids(thread_id))
        if reply is None:
            continue
        store.mark_outreach_replied(thread_info["id"])
        replies_found += 1
        _check_opt_out(reply.get("snippet", ""), thread_info["lead_email"], store)

    return {"replies": replies_found, "checked": len(sent_threads), "errors": errors}


def _fetch_thread_messages(gws_bin: str, env: dict, thread_id: str) -> list[dict] | None:
    params = json.dumps({"userId": "me", "id": thread_id, "format": "metadata"})
    try:
        result = subprocess.run(
            [gws_bin, "gmail", "users", "threads", "get", "--params", params],
            capture_output=True, text=True, env=env, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Could not read thread %s: %s", thread_id, exc)
        return None
    if result.returncode != 0:
        logger.warning("gws failed reading thread %s: %s", thread_id, result.stderr[:300])
        return None
    try:
        return json.loads(result.stdout).get("messages", [])
    except (json.JSONDecodeError, AttributeError):
        logger.warning("gws returned unreadable output for thread %s", thread_id)
        return None


def _first_reply(messages: list[dict], *, our_message_ids: set[str]) -> dict | None:
    for message in messages:
        if message.get("id") in our_message_ids or "SENT" in message.get("labelIds", []):
            continue
        return message
    return None


def get_due_followups(campaign_id: str, store: LeadStore | None = None) -> list[dict]:
    store = store or LeadStore()
    return store.get_outreach_due_followups(campaign_id)


def _send_via_gws(
    gws_bin: str,
    mailbox: dict,
    to: str,
    subject: str,
    body: str,
    reply_to_message_id: str | None = None,
) -> dict[str, Any]:
    if reply_to_message_id:
        cmd = [gws_bin, "gmail", "+reply-all", "--message-id", reply_to_message_id, "--body", body]
    else:
        cmd = [gws_bin, "gmail", "+send", "--to", to, "--subject", subject, "--body", body]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, env=gws_env(mailbox), timeout=30)
    except subprocess.TimeoutExpired:
        return {"error": "gws send timed out"}
    except OSError as exc:
        return {"error": str(exc)}

    if result.returncode != 0:
        return {"error": result.stderr[:300] or f"gws exited with status {result.returncode}"}

    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"id": "", "threadId": ""}
    return parsed if isinstance(parsed, dict) else {"id": "", "threadId": ""}


def _check_opt_out(snippet: str, email: str, store: LeadStore) -> None:
    text = snippet.lower()
    if any(signal in text for signal in OPT_OUT_SIGNALS):
        store.add_suppression(email, reason="opt_out_reply", source="auto_detected")
        logger.info("Auto-suppressed %s (opt-out detected in reply)", email)
