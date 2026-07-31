from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from system_app.models import (
    AgentAsyncCallbackReceipt,
    AgentJob,
)
from system_app.services.audit_retention import (
    purge_expired_backend_audits,
)
from tests.helpers import build_system_engine


def test_backend_minimal_audits_expire_at_three_years_only():
    engine, _cleanup = build_system_engine("backend_audit_retention")
    factory = sessionmaker(bind=engine, future=True)
    now = datetime(2026, 7, 28, 12, 0)
    with factory() as session:
        session.add_all(
            [
                AgentAsyncCallbackReceipt(
                    request_id="req_0000000000000001",
                    callback_hash="a" * 64,
                    event_type="missed_dose",
                    result_status="failed",
                    processed_at=now - timedelta(days=1096),
                    expires_at=now - timedelta(days=1),
                ),
                AgentAsyncCallbackReceipt(
                    request_id="req_0000000000000002",
                    callback_hash="b" * 64,
                    event_type="daily_pattern",
                    result_status="completed",
                    processed_at=now,
                    expires_at=now + timedelta(days=1095),
                ),
                AgentJob(
                    request_id="req_0000000000000003",
                    job_type="daily_pattern",
                    status="done",
                    payload_json="{}",
                ),
            ]
        )
        session.commit()

    result = purge_expired_backend_audits(factory, now=now)

    assert result == {
        "deleted_callback_receipts": 1,
    }
    with factory() as session:
        assert session.scalar(
            select(func.count()).select_from(
                AgentAsyncCallbackReceipt
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(AgentJob)
        ) == 1
