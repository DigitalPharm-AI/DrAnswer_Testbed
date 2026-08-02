from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import agent_app.main as agent_main
from agent_app.jobs.readiness import agent_ops_readiness_payload
from agent_app.jobs.status import mark_worker_started
from agent_app.jobs.tasks import DEAD, enqueue_async_task
from agent_app.persistence import db as agent_db
from agent_app.persistence.models import AgentAsyncTask
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.settings import get_settings
from shared.time_utils import utc_now
from tests.helpers import build_agent_engine


def build_agent_session():
    engine, _cleanup = build_agent_engine("agent_ops_readiness")
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
            request_id="push_message:notification:callback-failed",
            task_type="push_message",
            payload={"message": "증상 원문", "patient_id": "patient-private"},
            max_attempts=1,
        )
        callback_task = session.query(AgentAsyncTask).filter_by(
            request_id="push_message:notification:callback-failed"
        ).one()
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


def test_agent_db_routes_run_as_sync_endpoints():
    db_route_handlers = (
        agent_main.async_missed_dose_events,
        agent_main.async_push_messages,
        agent_main.async_clinician_alerts,
        agent_main.async_task_status,
        agent_main.agent_ops_readiness,
        agent_main.async_tasks,
        agent_main.dead_async_tasks,
        agent_main.async_task_action,
        agent_main.async_task_detail,
    )

    assert all(not inspect.iscoroutinefunction(handler) for handler in db_route_handlers)
    assert all("session" not in inspect.signature(handler).parameters for handler in db_route_handlers)


def test_agent_readiness_returns_connections_before_worker_reuse(tmp_path, monkeypatch):
    isolation_engine, cleanup = build_agent_engine(
        "agent_readiness_concurrency"
    )
    engine = create_engine(
        isolation_engine.url,
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        future=True,
    )
    isolation_engine.dispose()
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(agent_main, "SessionLocal", session_factory)
    monkeypatch.setattr(agent_db, "SessionLocal", session_factory)

    class HealthyBackendQueries:
        def verify_contract(self):
            return {
                "ok": True,
                "contract_version": BACKEND_READ_CONTRACT_VERSION,
                "dialect": "postgresql",
                "read_only": True,
                "views": [],
            }

    monkeypatch.setattr(
        agent_main.mcp_tool_server,
        "backend_queries",
        HealthyBackendQueries(),
    )

    async def send_concurrent_requests():
        token = get_settings().internal_api_token
        headers = {"X-Internal-Api-Token": token} if token else {}
        transport = httpx.ASGITransport(app=agent_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
            requests = [client.get("/agent/ops/readiness") for _ in range(50)]
            return await asyncio.wait_for(asyncio.gather(*requests), timeout=5)

    try:
        responses = asyncio.run(send_concurrent_requests())
        assert all(response.is_success for response in responses)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
        cleanup()


def test_agent_readiness_fails_closed_when_backend_read_contract_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(agent_main.mcp_tool_server, "backend_queries", None)

    token = get_settings().internal_api_token
    headers = {"X-Internal-Api-Token": token} if token else {}
    transport = httpx.ASGITransport(app=agent_main.app)

    async def request_readiness():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            headers=headers,
        ) as client:
            return await client.get("/agent/ops/readiness")

    response = asyncio.run(request_readiness())

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "critical"
    assert payload["backend_read"] == {
        "ok": False,
        "error": "backend_read_database_url_not_configured",
    }
    assert {
        alert["code"] for alert in payload["alerts"]
    } >= {"backend_read_contract_unavailable"}
