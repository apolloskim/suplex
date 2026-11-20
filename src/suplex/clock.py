"""Time helper.

Every timestamp Suplex stores in SQLite is a naive ISO-8601 string in UTC.
``datetime.utcnow()`` produced exactly that but is deprecated, so this is the
one place that knows how to build the same value from an aware datetime.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current time as a naive datetime in UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
