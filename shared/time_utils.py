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


def require_aware_datetime(value: datetime) -> datetime:
    """Reject contract timestamps that do not include a UTC offset."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


def require_optional_aware_datetime(
    value: datetime | None,
) -> datetime | None:
    return require_aware_datetime(value) if value is not None else None
