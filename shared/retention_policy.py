from __future__ import annotations

from datetime import datetime, timedelta


# Logs and Agent execution evidence have one fixed retention rule. 1095 days
# is deliberately used instead of a calendar-year replacement so every row
# receives the same deterministic expiry interval.
AGENT_OBSERVABILITY_RETENTION_YEARS = 3
AGENT_OBSERVABILITY_RETENTION_DAYS = 365 * (
    AGENT_OBSERVABILITY_RETENTION_YEARS
)
AGENT_OBSERVABILITY_RETENTION_SECONDS = (
    AGENT_OBSERVABILITY_RETENTION_DAYS * 24 * 60 * 60
)
def agent_observability_expires_at(recorded_at: datetime) -> datetime:
    """Return the fixed deletion/anonymization deadline for Agent evidence."""

    return recorded_at + timedelta(
        seconds=AGENT_OBSERVABILITY_RETENTION_SECONDS
    )
