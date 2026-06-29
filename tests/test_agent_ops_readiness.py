from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent_app.async_tasks import DEAD, enqueue_async_task
from agent_app.models import AgentAsyncTask
from agent_app.models import Base as AgentBase
from agent_app.ops_readiness import agent_ops_readiness_payload
from agent_app.worker_status import mark_worker_started
from shared.time_utils import utc_now


def build_agent_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    AgentBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def test_agent_ops_readiness_reports_ok_with_running_worker_and_empty_queue():
    with build_agent_session() as session:
        mark_worker_started(session, "worker-a")
        session.commit()

        payload = agent_ops_readiness_payload(session)

    assert payload["status"] == "ok"
    assert payload["alerts"] == []
    assert payload["metrics"]["running_worker_count"] == 1
    assert payload["runbook"] == "docs/PRODUCTION_READINESS.md#incident-runbook"


def test_agent_ops_readiness_flags_dead_callback_and_provider_failures():
    with build_agent_session() as session:
        enqueue_async_task(
            session,
            request_id="chat_continuation:notification:callback-failed",
            task_type="chat_continuation",
            payload={"message": "증상 원문", "phr_patient_key": "secret"},
            max_attempts=1,
        )
        callback_task = session.query(AgentAsyncTask).filter_by(request_id="chat_continuation:notification:callback-failed").one()
        callback_task.status = DEAD
        callback_task.attempts = 1
        callback_task.completed_at = utc_now()
        callback_task.last_error = "HTTPStatusError: callback 500 token=secret-value"

        enqueue_async_task(
            session,
            request_id="missed_dose:job:provider-failed",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
            max_attempts=1,
        )
        provider_task = session.query(AgentAsyncTask).filter_by(request_id="missed_dose:job:provider-failed").one()
        provider_task.status = DEAD
        provider_task.attempts = 1
        provider_task.completed_at = utc_now()
        provider_task.last_error = "Bedrock provider request failed"

        old_pending, _created = enqueue_async_task(
            session,
            request_id="daily_pattern:job:old-pending",
            task_type="daily_pattern",
            payload={"dose_event_id": 2},
        )
        old_pending.accepted_at = utc_now() - timedelta(seconds=999)
        session.commit()

        payload = agent_ops_readiness_payload(session)

    codes = {alert["code"] for alert in payload["alerts"]}
    assert payload["status"] == "critical"
    assert "agent_async_worker_unavailable" in codes
    assert "agent_async_dead_tasks_present" in codes
    assert "agent_async_callback_failures_detected" in codes
    assert "agent_provider_failures_detected" in codes
    assert "agent_async_pending_age_exceeded" in codes
    assert payload["metrics"]["callback_failure_count"] == 1
    assert payload["metrics"]["provider_failure_count"] == 1
    assert "secret-value" not in str(payload["dead_task_samples"])
