from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import system_app.routes.notifications as notifications_routes
from shared.settings import get_settings
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import AgentJob, Notification
from system_app.services.agent_jobs import FAILED, PENDING, RETRY_REQUESTED


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _create_retry_case(*, status: str = FAILED, request_id: str | None = None) -> tuple[int, int, str]:
    retry_request_id = request_id or f"retry-contract-{uuid4()}"
    with SessionLocal() as session:
        job = AgentJob(
            job_type="daily_pattern",
            status=status,
            payload_json="{}",
            error_message="original remote failure",
        )
        session.add(job)
        session.flush()
        notification = Notification(
            patient_id=get_settings().patient_id,
            notification_type="agent_error",
            title="AI 에이전트 오류",
            body="다시 시도할 수 있습니다.",
            visible_at=datetime(2026, 7, 26, 12, 0),
            acknowledged=False,
            metadata_json=json.dumps(
                {
                    "agent_job_id": job.id,
                    "request_id": retry_request_id,
                }
            ),
        )
        session.add(notification)
        session.commit()
        return job.id, notification.id, retry_request_id


def _delete_retry_case(job_id: int, notification_id: int) -> None:
    with SessionLocal() as session:
        session.query(Notification).filter(Notification.id == notification_id).delete()
        session.query(AgentJob).filter(AgentJob.id == job_id).delete()
        session.commit()


def test_remote_retry_failure_restores_failed_job_and_keeps_notification_actionable(monkeypatch):
    job_id, notification_id, request_id = _create_retry_case()
    calls: list[tuple[str, str, str]] = []

    async def reject_retry(received_request_id: str, action: str, reason: str = ""):
        calls.append((received_request_id, action, reason))
        return {"success": False, "message": "remote task is still unavailable"}

    monkeypatch.setattr(notifications_routes, "post_agent_task_action", reject_retry)

    try:
        response = TestClient(app).post(f"/agent-jobs/{job_id}/retry?notification_id={notification_id}")

        assert response.status_code == 502
        assert response.json()["detail"] == "agent_task_retry_failed"
        assert calls == [(request_id, "retry", "retry from agent error notification")]
        with SessionLocal() as session:
            job = session.get(AgentJob, job_id)
            notification = session.get(Notification, notification_id)
            assert job is not None
            assert job.status == FAILED
            assert job.error_message == "original remote failure"
            assert notification is not None
            assert notification.acknowledged is False
    finally:
        _delete_retry_case(job_id, notification_id)


def test_remote_retry_exception_restores_failed_job_and_keeps_notification_actionable(monkeypatch):
    job_id, notification_id, _request_id = _create_retry_case()

    async def raise_during_retry(*_args, **_kwargs):
        raise RuntimeError("simulated transport failure")

    monkeypatch.setattr(notifications_routes, "post_agent_task_action", raise_during_retry)

    try:
        response = TestClient(app).post(f"/agent-jobs/{job_id}/retry?notification_id={notification_id}")

        assert response.status_code == 502
        assert response.json()["detail"] == "agent_task_retry_failed"
        with SessionLocal() as session:
            job = session.get(AgentJob, job_id)
            notification = session.get(Notification, notification_id)
            assert job is not None
            assert job.status == FAILED
            assert job.error_message == "original remote failure"
            assert notification is not None
            assert notification.acknowledged is False
    finally:
        _delete_retry_case(job_id, notification_id)


def test_retry_rejects_in_progress_and_stale_jobs_without_remote_call(monkeypatch):
    in_progress_job_id, in_progress_notification_id, _ = _create_retry_case(status=RETRY_REQUESTED)
    stale_job_id, stale_notification_id, _ = _create_retry_case(status=PENDING)
    calls: list[str] = []

    async def unexpected_remote_call(request_id: str, *_args, **_kwargs):
        calls.append(request_id)
        return {"success": True}

    monkeypatch.setattr(notifications_routes, "post_agent_task_action", unexpected_remote_call)

    try:
        client = TestClient(app)
        in_progress_response = client.post(
            f"/agent-jobs/{in_progress_job_id}/retry?notification_id={in_progress_notification_id}"
        )
        stale_response = client.post(f"/agent-jobs/{stale_job_id}/retry?notification_id={stale_notification_id}")

        assert in_progress_response.status_code == 409
        assert in_progress_response.json()["detail"] == "agent_job_retry_in_progress"
        assert stale_response.status_code == 409
        assert stale_response.json()["detail"] == "agent_job_retry_stale"
        assert calls == []
        with SessionLocal() as session:
            in_progress_notification = session.get(Notification, in_progress_notification_id)
            stale_notification = session.get(Notification, stale_notification_id)
            assert in_progress_notification is not None
            assert in_progress_notification.acknowledged is False
            assert stale_notification is not None
            assert stale_notification.acknowledged is False
    finally:
        _delete_retry_case(in_progress_job_id, in_progress_notification_id)
        _delete_retry_case(stale_job_id, stale_notification_id)


def test_retry_error_codes_have_user_facing_browser_feedback() -> None:
    htmx_script = (PROJECT_ROOT / "system_app" / "static" / "htmx-lite.js").read_text(encoding="utf-8")

    assert "agent_job_retry_in_progress:" in htmx_script
    assert "agent_job_retry_stale:" in htmx_script
    assert "agent_task_retry_failed:" in htmx_script
