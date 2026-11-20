"""Outreach playbook — local markdown file the agent reads when drafting emails.

The playbook captures the user's outreach personality: tone, angle, CTA,
footer, audience criteria. The agent surfaces and edits it through conversation.
"""

from __future__ import annotations

from pathlib import Path

PLAYBOOK_DIR = Path.home() / ".config" / "suplex"
PLAYBOOK_PATH = PLAYBOOK_DIR / "playbook.md"

DEFAULT_PLAYBOOK = """\
# Outreach Playbook

## Tone
- Casual and direct, not salesy
- Under 4 sentences for the body
- Write like a real person, not a marketing template

## Angle
- Reference something specific about the lead (review, post, headline)
- Lead with what THEY care about, not what you sell
- Show you did your homework

## CTA
- Low-friction ask: "Would a quick 10-minute chat make sense this week?"
- One CTA per email, never two

## What to avoid
- No "I hope this finds you well"
- No discount offers in first touch
- No competitor mentions
- No buzzwords (synergy, leverage, disrupt)

## Footer
- Always include a conversational opt-out line
- Example: "PS — Not relevant? Just let me know and I'll stop reaching out."
- Always include physical address on a separate line

## Audience
- Only leads with valid emails
- Prefer leads with enrichment data (reviews, posts, website)
- Skip leads with no company name

## Follow-up style
- Follow-up 1 (day 3): shorter, different angle, same thread
- Follow-up 2 (day 7): brief breakup email — friendly, no guilt
- Never repeat the same hook across steps
"""


def get_playbook() -> str | None:
    """Read the current playbook. Returns None if not created yet."""
    if PLAYBOOK_PATH.exists():
        return PLAYBOOK_PATH.read_text()
    return None


def get_playbook_summary() -> str:
    """One-line summary of the playbook for agent to show before drafting."""
    text = get_playbook()
    if not text:
        return "No playbook configured. Run `suplex playbook init` to create one."

    # Extract key sections for summary.
    parts = []
    for section in ["Tone", "CTA", "Angle"]:
        start = text.find(f"## {section}")
        if start == -1:
            continue
        end = text.find("\n## ", start + 1)
        block = text[start:end] if end != -1 else text[start:]
        # Grab first bullet point.
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("- "):
                parts.append(line[2:])
                break

    return "; ".join(parts) if parts else "Playbook exists but could not extract summary."


def init_playbook(content: str | None = None) -> Path:
    """Create the playbook file. Returns the path."""
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOK_PATH.write_text(content or DEFAULT_PLAYBOOK)
    return PLAYBOOK_PATH


def update_playbook(content: str) -> Path:
    """Overwrite the playbook with new content."""
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    PLAYBOOK_PATH.write_text(content)
    return PLAYBOOK_PATH
