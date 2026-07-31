from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session, sessionmaker

from shared.time_utils import utc_now
from system_app.models import AgentAsyncCallbackReceipt


def purge_expired_backend_audits(
    session_factory: sessionmaker[Session],
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """Delete only expired minimal API/audit rows.

    Chat messages, Agent jobs, callback-applied domain records, and other
    Backend business state are intentionally outside this cleanup.
    """

    current = now or utc_now()
    with session_factory() as session:
        deleted_callback_receipts = session.execute(
            delete(AgentAsyncCallbackReceipt).where(
                AgentAsyncCallbackReceipt.expires_at <= current
            )
        ).rowcount
        session.commit()
    return {
        "deleted_callback_receipts": int(
            deleted_callback_receipts or 0
        ),
    }
