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


def as_simulation_naive_datetime(value: datetime) -> datetime:
    """Normalize a timestamp to the testbed DB's naive Seoul wall time.

    The v1.3 boundary accepts timezone-aware instants, while the simulation
    database intentionally stores local wall-clock values without tzinfo.
    """

    return as_seoul_datetime(value).replace(tzinfo=None)


def naive_utc_as_seoul(value: datetime) -> datetime:
    """Interpret a naive persistence timestamp as UTC, then display in Seoul."""

    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(SEOUL)
