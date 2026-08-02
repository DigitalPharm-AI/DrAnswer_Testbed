"""Contract tests for the active v1.3 asynchronous medication boundary."""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from agent_app import main as agent_main
from agent_app.openapi_v13 import (
    build_agent_v13_async_medication_openapi,
)
from agent_app.persistence.db import SessionLocal as AgentSessionLocal
from agent_app.persistence.models import AgentAsyncTask
from shared.schemas import MissedDoseEventPayload
from system_app import main as system_main
from system_app.db import SessionLocal as SystemSessionLocal
from system_app.models import (
    AgentAsyncCallbackReceipt,
    AgentJob,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    Notification,
    NotificationPolicyChangeProposal,
    ReminderPolicy,
)
from system_app.openapi_v13 import (
    build_backend_v13_async_callback_openapi,
)
from system_app.routes import agent_async_api as agent_async_api_routes
from system_app.services.agent_jobs import RUNNING, create_agent_job
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import (
    create_missed_dose_conversation_alert,
)
from system_app.services.workers import external_agent_request

AGENT_HEADERS = {
    "Authorization": "Bearer pytest-agent-sync-token",
}
BACKEND_HEADERS = {
    "Authorization": "Bearer pytest-agent-sync-token",
}
DATABASE_TESTS_CONFIGURED = bool(
    os.getenv("SYSTEM_POSTGRES_TEST_DATABASE_URL")
    and os.getenv("AGENT_POSTGRES_TEST_DATABASE_URL")
)
requires_postgres = pytest.mark.skipif(
    not DATABASE_TESTS_CONFIGURED,
    reason="isolated PostgreSQL test databases are not configured",
)


def _request_id() -> str:
    return f"req_{uuid4().hex[:16]}"


def _patient_id() -> str:
    return f"patient_{uuid4().hex[:16]}"


def _assert_error_envelope(
    response,
    *,
    request_id: str | None,
    code: str,
) -> dict:
    body = response.json()
    assert set(body) == {"request_id", "error"}
    assert body["request_id"] == request_id
    assert set(body["error"]) == {
        "code",
        "message",
        "retryable",
        "details",
    }
    assert body["error"]["code"] == code
    return body


def _missed_dose_acceptance_body(
    request_id: str,
    *,
    patient_id: str | None = None,
    dose_event_id: str | None = None,
) -> dict[str, str]:
    return {
        "request_id": request_id,
        "patient_id": patient_id or _patient_id(),
        "dose_event_id": dose_event_id or f"dose_{uuid4().hex[:16]}",
    }


@requires_postgres
def test_missed_dose_acceptance_is_minimal_and_idempotent() -> None:
    body = _missed_dose_acceptance_body(_request_id())
    client = TestClient(agent_main.app)

    first = client.post(
        "/agent/async/missed-dose-events",
        headers=AGENT_HEADERS,
        json=body,
    )
    duplicate = client.post(
        "/agent/async/missed-dose-events",
        headers=AGENT_HEADERS,
        json=body,
    )

    assert first.status_code == 202
    assert first.json() == {
        "request_id": body["request_id"],
        "status": "accepted",
    }
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "request_id": body["request_id"],
        "status": "duplicate",
    }

    conflict_body = {
        **body,
        "dose_event_id": f"dose_{uuid4().hex[:16]}",
    }
    conflict = client.post(
        "/agent/async/missed-dose-events",
        headers=AGENT_HEADERS,
        json=conflict_body,
    )
    assert conflict.status_code == 409
    _assert_error_envelope(
        conflict,
        request_id=body["request_id"],
        code="IDEMPOTENCY_CONFLICT",
    )

    with AgentSessionLocal() as session:
        task = session.scalar(
            select(AgentAsyncTask).where(
                AgentAsyncTask.request_id == body["request_id"]
            )
        )
        assert task is not None
        assert task.task_type == "missed_dose"
        stored = json.loads(task.payload_json)
        assert {
            key: stored[key]
            for key in ("request_id", "patient_id", "dose_event_id")
        } == body


def test_missed_dose_acceptance_rejects_extra_fields_and_old_path() -> None:
    body = {
        **_missed_dose_acceptance_body(_request_id()),
        "requested_at": "2026-07-28T09:30:00+09:00",
    }
    client = TestClient(agent_main.app)

    extra = client.post(
        "/agent/async/missed-dose-events",
        headers=AGENT_HEADERS,
        json=body,
    )
    old_path = client.post(
        "/agent/async/medication-events",
        headers=AGENT_HEADERS,
        json=body,
    )

    assert extra.status_code == 400
    _assert_error_envelope(
        extra,
        request_id=body["request_id"],
        code="INVALID_REQUEST",
    )
    assert old_path.status_code == 404


@requires_postgres
def test_backend_mapper_exposes_only_minimal_missed_dose_fields() -> None:
    patient_id = _patient_id()
    with SystemSessionLocal() as session:
        event = _seed_dose_event(session, patient_id)
        create_missed_dose_conversation_alert(
            session,
            event,
            event.scheduled_for + timedelta(minutes=90),
        )
        payload = MissedDoseEventPayload(
            patient_id=patient_id,
            dose_event_id=event.id,
            medication_name=event.medication_name,
            slot_label=event.slot_label,
            scheduled_for=event.scheduled_for,
            detected_at=event.scheduled_for + timedelta(minutes=90),
        )
        job = create_agent_job(session, "missed_dose", payload)
        mapped = external_agent_request(session, job, payload)
        serialized = mapped.model_dump(mode="json")
        request_id = job.request_id
        public_dose_event_id = event.public_id
        session.rollback()

    assert serialized == {
        "request_id": request_id,
        "patient_id": patient_id,
        "dose_event_id": public_dose_event_id,
    }


@requires_postgres
def test_missed_dose_callback_is_minimal_idempotent_and_conflict_safe() -> None:
    patient_id = _patient_id()
    with SystemSessionLocal() as session:
        event = _seed_dose_event(session, patient_id)
        payload = MissedDoseEventPayload(
            patient_id=patient_id,
            dose_event_id=event.id,
            medication_name=event.medication_name,
            slot_label=event.slot_label,
            scheduled_for=event.scheduled_for,
            detected_at=event.scheduled_for + timedelta(minutes=90),
        )
        job = create_agent_job(session, "missed_dose", payload)
        job.status = RUNNING
        request_id = job.request_id
        dose_event_row_id = event.id
        session.commit()

    body = {
        "request_id": request_id,
        "status": "completed",
        "result": {
            "message": "현재 복용 가능한 상태인지 알려주세요.",
            "requires_reply": True,
        },
        "error": None,
    }
    client = TestClient(system_main.create_app())
    first = client.post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json=body,
    )
    duplicate = client.post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json=body,
    )

    assert first.status_code == 200
    assert first.json() == {
        "request_id": request_id,
        "status": "processed",
    }
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "request_id": request_id,
        "status": "duplicate",
    }

    conflict_body = json.loads(json.dumps(body))
    conflict_body["result"]["message"] = "다른 최종 안내 문장"
    conflict = client.post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json=conflict_body,
    )
    assert conflict.status_code == 409
    _assert_error_envelope(
        conflict,
        request_id=request_id,
        code="CALLBACK_STATE_CONFLICT",
    )

    with SystemSessionLocal() as session:
        stored_job = session.scalar(
            select(AgentJob).where(AgentJob.request_id == request_id)
        )
        receipt = session.get(AgentAsyncCallbackReceipt, request_id)
        message_count = session.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.related_dose_event_id == dose_event_row_id,
                ChatMessage.content
                == "현재 복용 가능한 상태인지 알려주세요.",
            )
        )
        assert stored_job is not None
        assert stored_job.status == "done"
        assert receipt is not None
        assert receipt.event_type == "missed_dose"
        assert message_count == 1


@requires_postgres
def test_missed_dose_callback_suppresses_delivery_when_dose_was_taken() -> None:
    patient_id = _patient_id()
    with SystemSessionLocal() as session:
        event = _seed_dose_event(session, patient_id)
        alert = create_missed_dose_conversation_alert(
            session,
            event,
            event.scheduled_for + timedelta(minutes=90),
        )
        payload = MissedDoseEventPayload(
            patient_id=patient_id,
            dose_event_id=event.id,
            medication_name=event.medication_name,
            slot_label=event.slot_label,
            scheduled_for=event.scheduled_for,
            detected_at=event.scheduled_for + timedelta(minutes=90),
        )
        job = create_agent_job(session, "missed_dose", payload)
        job.status = RUNNING
        event.status = "taken"
        request_id = job.request_id
        alert_id = alert.id
        dose_event_row_id = event.id
        session.commit()

    response = TestClient(system_main.create_app()).post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json={
            "request_id": request_id,
            "status": "completed",
            "result": {
                "message": "복약을 놓친 상황을 확인하고 싶어요.",
                "requires_reply": True,
            },
            "error": None,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "request_id": request_id,
        "status": "processed",
    }

    with SystemSessionLocal() as session:
        stored_job = session.scalar(
            select(AgentJob).where(AgentJob.request_id == request_id)
        )
        stored_alert = session.get(Notification, alert_id)
        message_count = session.scalar(
            select(func.count(ChatMessage.id)).where(
                ChatMessage.related_dose_event_id == dose_event_row_id,
            )
        )
        metadata = json.loads(stored_alert.metadata_json)

        assert stored_job is not None
        assert stored_job.status == "done"
        assert stored_alert is not None
        assert stored_alert.acknowledged is True
        assert metadata["status"] == "superseded"
        assert metadata["superseded_reason"] == "dose_status_changed_to_taken"
        assert metadata["request_id"] == request_id
        assert message_count == 0


@requires_postgres
def test_missed_dose_callback_conflict_hides_backend_job_state() -> None:
    patient_id = _patient_id()
    with SystemSessionLocal() as session:
        event = _seed_dose_event(session, patient_id)
        payload = MissedDoseEventPayload(
            patient_id=patient_id,
            dose_event_id=event.id,
            medication_name=event.medication_name,
            slot_label=event.slot_label,
            scheduled_for=event.scheduled_for,
            detected_at=event.scheduled_for + timedelta(minutes=90),
        )
        job = create_agent_job(session, "missed_dose", payload)
        job.status = "done"
        request_id = job.request_id
        session.commit()

    response = TestClient(system_main.create_app()).post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json={
            "request_id": request_id,
            "status": "completed",
            "result": {
                "message": "현재 상태를 알려주세요.",
                "requires_reply": True,
            },
            "error": None,
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {
        "request_id": request_id,
    }
    serialized = response.text
    assert "job_type" not in serialized
    assert "job_status" not in serialized
    assert "missed_dose" not in serialized
    assert "done" not in serialized


def test_missed_dose_callback_validates_result_error_pair() -> None:
    request_id = _request_id()
    response = TestClient(system_main.create_app()).post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json={
            "request_id": request_id,
            "status": "completed",
            "result": None,
            "error": None,
        },
    )
    assert response.status_code == 400
    _assert_error_envelope(
        response,
        request_id=request_id,
        code="INVALID_REQUEST",
    )

    extra_error_field = TestClient(system_main.create_app()).post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json={
            "request_id": request_id,
            "status": "failed",
            "result": None,
            "error": {
                "code": "AI_PROCESSING_ERROR",
                "message": "Processing failed.",
                "retryable": True,
                "details": None,
            },
        },
    )
    assert extra_error_field.status_code == 400
    _assert_error_envelope(
        extra_error_field,
        request_id=request_id,
        code="INVALID_REQUEST",
    )


def test_async_callback_unexpected_error_uses_exact_backend_message(
    monkeypatch,
) -> None:
    def fail_unexpectedly(*_args, **_kwargs):
        raise RuntimeError("internal diagnostic must not escape")

    monkeypatch.setattr(
        agent_async_api_routes,
        "process_missed_dose_result_callback",
        fail_unexpectedly,
    )
    request_id = _request_id()
    response = TestClient(system_main.create_app()).post(
        "/api/agent/async/missed-dose-results",
        headers=BACKEND_HEADERS,
        json={
            "request_id": request_id,
            "status": "completed",
            "result": {
                "message": "현재 상태를 알려주세요.",
                "requires_reply": True,
            },
            "error": None,
        },
    )

    assert response.status_code == 500
    assert response.json() == {
        "request_id": request_id,
        "error": {
            "code": "BACKEND_PROCESSING_ERROR",
            "message": (
                "An internal Backend Server processing error occurred."
            ),
            "retryable": True,
            "details": None,
        },
    }


@requires_postgres
def test_policy_proposal_requires_confirmation_and_never_applies_directly() -> None:
    patient_id = _patient_id()
    with SystemSessionLocal() as session:
        clock = ensure_clock(session)
        policy = ReminderPolicy(
            patient_id=patient_id,
            policy_key="morning",
            slot_label="아침",
            extra_reminders=1,
            interval_minutes=10,
            missed_dose_after_minutes=30,
            primary_reminder_timing="at",
            primary_reminder_offset_minutes=0,
            effective_start_date=clock.current_time.date()
            - timedelta(days=1),
            effective_end_date=clock.current_time.date()
            + timedelta(days=30),
            reason="test baseline",
            source="system_request",
            active=True,
            version=1,
        )
        session.add(policy)
        session.commit()
        policy_id = policy.id

    request_id = _request_id()
    body = {
        "request_id": request_id,
        "patient_id": patient_id,
        "proposed_policy": {
            "extra_reminders": 3,
            "interval_minutes": 15,
            "missed_dose_after_minutes": 45,
        },
        "reason": "최근 아침 미복용 빈도가 증가했습니다.",
    }
    client = TestClient(system_main.create_app())
    first = client.post(
        "/api/agent/async/notification-policy-change-proposals",
        headers=BACKEND_HEADERS,
        json=body,
    )
    duplicate = client.post(
        "/api/agent/async/notification-policy-change-proposals",
        headers=BACKEND_HEADERS,
        json=body,
    )

    assert first.status_code == 202
    assert first.json() == {
        "request_id": request_id,
        "status": "accepted",
    }
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "request_id": request_id,
        "status": "duplicate",
    }

    conflict_body = json.loads(json.dumps(body))
    conflict_body["proposed_policy"]["extra_reminders"] = 4
    conflict = client.post(
        "/api/agent/async/notification-policy-change-proposals",
        headers=BACKEND_HEADERS,
        json=conflict_body,
    )
    assert conflict.status_code == 409
    _assert_error_envelope(
        conflict,
        request_id=request_id,
        code="IDEMPOTENCY_CONFLICT",
    )

    with SystemSessionLocal() as session:
        policy = session.get(ReminderPolicy, policy_id)
        proposal = session.get(
            NotificationPolicyChangeProposal,
            request_id,
        )
        notification_count = session.scalar(
            select(func.count(Notification.id)).where(
                Notification.patient_id == patient_id,
                Notification.notification_type == "conversation_alert",
            )
        )
        assert policy is not None
        assert policy.extra_reminders == 1
        assert policy.interval_minutes == 10
        assert policy.missed_dose_after_minutes == 30
        assert policy.version == 1
        assert proposal is not None
        assert proposal.status == "pending_user_confirmation"
        assert notification_count == 1


def test_policy_proposal_rejects_empty_or_extra_policy_fields() -> None:
    client = TestClient(system_main.create_app())
    base = {
        "request_id": _request_id(),
        "patient_id": _patient_id(),
        "proposed_policy": {},
        "reason": "변경 제안",
    }
    empty = client.post(
        "/api/agent/async/notification-policy-change-proposals",
        headers=BACKEND_HEADERS,
        json=base,
    )
    extra_body = json.loads(json.dumps(base))
    extra_body["request_id"] = _request_id()
    extra_body["proposed_policy"] = {"policy_id": "npol_00000000deadbeef"}
    extra = client.post(
        "/api/agent/async/notification-policy-change-proposals",
        headers=BACKEND_HEADERS,
        json=extra_body,
    )
    assert empty.status_code == 422
    _assert_error_envelope(
        empty,
        request_id=base["request_id"],
        code="INVALID_POLICY_PROPOSAL",
    )
    assert extra.status_code == 400
    _assert_error_envelope(
        extra,
        request_id=extra_body["request_id"],
        code="INVALID_REQUEST",
    )


def test_async_openapi_exports_match_runtime_minimal_contracts() -> None:
    agent_export = build_agent_v13_async_medication_openapi(
        agent_main.app,
    )
    backend_export = build_backend_v13_async_callback_openapi(
        system_main.create_app(),
    )
    assert set(agent_export["paths"]) == {
        "/agent/async/missed-dose-events",
        "/agent/async/daily-medication-pattern-analysis",
    }
    assert set(backend_export["paths"]) == {
        "/api/agent/async/missed-dose-results",
        "/api/agent/async/notification-policy-change-proposals",
    }
    assert "/agent/async/medication-events" not in agent_main.app.openapi()[
        "paths"
    ]
    assert "/api/agent/async/job-results" not in system_main.create_app().openapi()[
        "paths"
    ]
    assert "/api/agent/async/failures" not in system_main.create_app().openapi()[
        "paths"
    ]
    missed_dose_responses = agent_export["paths"][
        "/agent/async/missed-dose-events"
    ]["post"]["responses"]
    assert {"200", "202"} <= set(missed_dose_responses)
    assert (
        missed_dose_responses["200"]["content"]["application/json"][
            "schema"
        ]["$ref"]
        == "#/components/schemas/AsyncEventAccepted"
    )
    assert (
        "422"
        not in missed_dose_responses
    )
    assert (
        "422"
        not in backend_export["paths"][
            "/api/agent/async/missed-dose-results"
        ]["post"]["responses"]
    )
    assert (
        "422"
        in backend_export["paths"][
            "/api/agent/async/notification-policy-change-proposals"
        ]["post"]["responses"]
    )

    acceptance = agent_export["components"]["schemas"][
        "MissedDoseEventRequest"
    ]
    assert set(acceptance["properties"]) == {
        "request_id",
        "patient_id",
        "dose_event_id",
    }
    assert acceptance["additionalProperties"] is False

    result = backend_export["components"]["schemas"][
        "MissedDoseResultCallback"
    ]
    assert set(result["properties"]) == {
        "request_id",
        "status",
        "result",
        "error",
    }
    assert result["additionalProperties"] is False
    callback_error = backend_export["components"]["schemas"][
        "AsyncProcessingError"
    ]
    assert set(callback_error["properties"]) == {
        "code",
        "message",
        "retryable",
    }
    assert set(callback_error["required"]) == {
        "code",
        "message",
        "retryable",
    }
    assert callback_error["additionalProperties"] is False

    proposal = backend_export["components"]["schemas"][
        "NotificationPolicyChangeProposalRequest"
    ]
    assert set(proposal["properties"]) == {
        "request_id",
        "patient_id",
        "proposed_policy",
        "reason",
    }
    assert proposal["additionalProperties"] is False

    agent_path = Path("docs/AI_V13_ASYNC_MEDICATION_OPENAPI.json")
    backend_path = Path("docs/BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json")
    assert json.loads(agent_path.read_text(encoding="utf-8")) == agent_export
    assert json.loads(backend_path.read_text(encoding="utf-8")) == backend_export


def _seed_dose_event(
    session,
    patient_id: str,
) -> DoseEvent:
    today = date(2026, 7, 28)
    plan = MedicationPlan(
        patient_id=patient_id,
        medication_name="암로디핀 5mg",
        start_date=today,
        end_date=today + timedelta(days=30),
        active=True,
    )
    session.add(plan)
    session.flush()
    schedule = DoseSchedule(
        plan_id=plan.id,
        slot_label="아침",
        scheduled_time="08:00",
    )
    session.add(schedule)
    session.flush()
    event = DoseEvent(
        patient_id=patient_id,
        plan_id=plan.id,
        schedule_id=schedule.id,
        medication_name=plan.medication_name,
        slot_label=schedule.slot_label,
        scheduled_for=datetime(2026, 7, 28, 8, 0),
        status="missed",
    )
    session.add(event)
    session.flush()
    return event
