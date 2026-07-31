from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    """Return naive UTC for existing DateTime columns without using deprecated utcnow()."""
    return datetime.now(UTC).replace(tzinfo=None)


def as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def as_naive_utc(value: datetime) -> datetime:
    return as_aware_utc(value).replace(tzinfo=None)
