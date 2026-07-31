from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

SEOUL = ZoneInfo("Asia/Seoul")


def as_seoul_datetime(value: datetime) -> datetime:
    """Normalize Backend timestamps for the Korean testbed display contract.

    Simulation DB values are intentionally naive local values. A timezone-aware
    value is treated as an absolute instant and converted to Asia/Seoul.
    """

    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=SEOUL)
    return value.astimezone(SEOUL)


def as_seoul_iso(value: datetime) -> str:
    return as_seoul_datetime(value).isoformat()


def naive_utc_as_seoul(value: datetime) -> datetime:
    """Interpret a naive persistence timestamp as UTC, then display in Seoul."""

    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SEOUL)
