from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for existing DateTime columns without using deprecated utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)
