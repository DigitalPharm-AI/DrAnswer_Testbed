import json
import logging
import threading
import time
from datetime import date, datetime

import pytest

import system_app.main as system_main
from shared.async_v13_contracts import AsyncEventAccepted
from shared.schemas import (
    MissedDoseEventPayload,
    SlotAdherenceSummary,
)
from shared.settings import get_settings
from system_app.models import (
    AgentJob,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    Notification,
)
from system_app.services.agent_client import AgentServiceError
from system_app.services.agent_jobs import FAILED, create_agent_job
from system_app.services.clock_service import ensure_clock
from system_app.services.notification_service import create_notification
from system_app.services.patient_profile_service import ensure_base_data
from tests.helpers import build_session, build_threadsafe_session_factory

TEST_PATIENT_ID = get_settings().patient_id


class FailingAgentClient:
    def __init__(self, error: AgentServiceError) -> None:
        self.error = error

    async def send_medication_event_async(self, _payload):
        raise self.error


class AcceptAfterUncertainDeliveryClient:
    def __init__(self, stop_event: threading.Event) -> None:
        self.stop_event = stop_event
        self.requests = []

    async def send_medication_event_async(self, payload):
        self.requests.append(payload)
        if len(self.requests) == 1:
            raise AgentServiceError(
                "acceptance response was lost",
                error_type="agent_network_error",
                retryable=True,
            )
        self.stop_event.set()
        return AsyncEventAccepted(
            request_id=payload.request_id,
            status="duplicate",
        )


def _seed_dose_parent(session) -> None:
    session.add(
        MedicationPlan(
            id=1,
            patient_id=TEST_PATIENT_ID,
            medication_name="test medication",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            active=True,
        )
    )
    session.flush()
    session.add(
        DoseSchedule(
            id=1,
            plan_id=1,
            slot_label="morning",
            scheduled_time="08:00",
        )
    )
    session.flush()


def _seed_dose_event(session, event_id: int) -> None:
    _seed_dose_parent(session)
    session.add(
        DoseEvent(
            id=event_id,
            patient_id=TEST_PATIENT_ID,
            plan_id=1,
            schedule_id=1,
            medication_name="test medication",
            slot_label="morning",
            scheduled_for=datetime(2026, 4, 20, 8, 0),
            status="missed",
        )
    )
    session.flush()


def test_backend_agent_job_rejects_daily_pattern_dispatch():
    with build_session() as session:
        with pytest.raises(
            ValueError,
            match="unsupported_external_agent_job_type",
        ):
            create_agent_job(
                session,
                "daily_pattern",
                object(),
            )
        assert session.query(AgentJob).count() == 0


def test_agent_worker_records_failure_without_stopping(monkeypatch):
    session_factory = build_threadsafe_session_factory()
    with session_factory() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.is_running = True
        clock.speed_multiplier = 10
        session.commit()

    failing_client = FailingAgentClient(
        AgentServiceError(
            "missed_dose_coach 모델 호출에 실패했습니다.",
            error_type="provider_request_failed",
            agent_name="missed_dose_coach",
            decision_type="missed_dose_assessment",
        )
    )
    payload = MissedDoseEventPayload(
        patient_id=TEST_PATIENT_ID,
        dose_event_id=33,
        medication_name="영양제",
        slot_label="야간 21:00",
        scheduled_for=datetime(2026, 4, 20, 21, 0),
        detected_at=datetime(2026, 4, 20, 21, 40),
        recent_slot_summaries=[
            SlotAdherenceSummary(
                slot_label="야간 21:00",
                scheduled_count=1,
                taken_count=0,
                missed_count=1,
                miss_rate=1.0,
            )
        ],
        chat_context=[],
    )
    with session_factory() as session:
        _seed_dose_parent(session)
        session.add(
            DoseEvent(
                id=33,
                patient_id=TEST_PATIENT_ID,
                plan_id=1,
                schedule_id=1,
                medication_name="영양제",
                slot_label="야간 21:00",
                scheduled_for=datetime(2026, 4, 20, 21, 0),
                status="missed",
            )
        )
        session.flush()
        job = create_agent_job(session, "missed_dose", payload)
        job_id = job.id
        alert = create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="AI가 상황을 확인하고 있어요.",
            visible_at=datetime(2026, 4, 20, 21, 40),
            related_dose_event_id=33,
            metadata={"status": "awaiting_agent"},
        )
        alert_id = alert.id
        session.commit()

    monkeypatch.setattr(system_main, "SessionLocal", session_factory)
    monkeypatch.setattr(system_main, "agent_client", failing_client)

    stop_event = threading.Event()
    worker = threading.Thread(
        target=system_main.agent_worker,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with system_main.write_lock:
            with session_factory() as session:
                job = session.get(AgentJob, job_id)
                if job and job.status == FAILED:
                    break
        time.sleep(0.05)

    assert worker.is_alive()
    stop_event.set()
    worker.join(timeout=2)
    assert not worker.is_alive()

    with system_main.write_lock:
        with session_factory() as session:
            job = session.get(AgentJob, job_id)
            assert job is not None
            assert job.status == FAILED
            clock = ensure_clock(session)
            assert clock.is_running is True
            assert clock.speed_multiplier == 10
            notification = (
                session.query(Notification)
                .filter(Notification.notification_type == "agent_error")
                .one()
            )
            assert "백그라운드 AI 작업을 처리하지 못했습니다." in notification.body
            alert = session.get(Notification, alert_id)
            assert alert is not None
            assert "AI가 미복용 상황을 처리하지 못했습니다." in alert.body
            metadata = json.loads(alert.metadata_json)
            assert metadata["status"] == "agent_error"
            assert metadata["agent_job_id"] == job_id
            assert "trace_id" not in metadata


def test_agent_worker_retries_uncertain_acceptance_with_same_request_id(
    monkeypatch,
):
    session_factory = build_threadsafe_session_factory()
    payload = MissedDoseEventPayload(
        patient_id=TEST_PATIENT_ID,
        dose_event_id=34,
        medication_name="혈압약",
        slot_label="아침",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        detected_at=datetime(2026, 4, 20, 9, 30),
    )
    with session_factory() as session:
        _seed_dose_parent(session)
        session.add(
            DoseEvent(
                id=34,
                patient_id=TEST_PATIENT_ID,
                plan_id=1,
                schedule_id=1,
                medication_name="혈압약",
                slot_label="아침",
                scheduled_for=payload.scheduled_for,
                status="missed",
            )
        )
        session.flush()
        job = create_agent_job(session, "missed_dose", payload)
        job_id = job.id
        request_id = job.request_id
        session.commit()

    stop_event = threading.Event()
    retrying_client = AcceptAfterUncertainDeliveryClient(stop_event)
    monkeypatch.setattr(system_main, "SessionLocal", session_factory)
    monkeypatch.setattr(system_main, "agent_client", retrying_client)

    worker = threading.Thread(
        target=system_main.agent_worker,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()
    worker.join(timeout=3)

    assert not worker.is_alive()
    assert len(retrying_client.requests) == 2
    assert {
        request.request_id for request in retrying_client.requests
    } == {request_id}
    first = retrying_client.requests[0].model_dump(
        mode="json",
    )
    second = retrying_client.requests[1].model_dump(
        mode="json",
    )
    assert first == second
    with session_factory() as session:
        retried_job = session.get(AgentJob, job_id)
        assert retried_job is not None
        assert retried_job.status == "running"
        assert retried_job.attempts == 2


def test_create_agent_job_deduplicates_active_missed_dose_job():
    with build_session() as session:
        _seed_dose_event(session, 77)
        payload = MissedDoseEventPayload(
            patient_id=TEST_PATIENT_ID,
            dose_event_id=77,
            medication_name="당뇨약",
            slot_label="야간 21:00",
            scheduled_for=datetime(2026, 4, 20, 21, 0),
            detected_at=datetime(2026, 4, 20, 22, 30),
            recent_slot_summaries=[],
            chat_context=[],
        )

        first_job = create_agent_job(session, "missed_dose", payload)
        second_job = create_agent_job(session, "missed_dose", payload)

        assert second_job.id == first_job.id
        assert session.query(AgentJob).count() == 1


def test_agent_worker_marks_invalid_payload_failed_without_stopping(
    monkeypatch,
    caplog,
):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    session_factory = build_threadsafe_session_factory()
    payload = MissedDoseEventPayload(
        patient_id=TEST_PATIENT_ID,
        dose_event_id=44,
        medication_name="혈압약",
        slot_label="아침 08:00",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        detected_at=datetime(2026, 4, 20, 9, 30),
        recent_slot_summaries=[],
        chat_context=[],
    )

    with session_factory() as session:
        ensure_base_data(session)
        _seed_dose_event(session, 44)
        clock = ensure_clock(session)
        job = create_agent_job(session, "missed_dose", payload)
        job.payload_json = "{invalid-json"
        job_id = job.id
        alert = create_notification(
            session,
            notification_type="conversation_alert",
            title="미복용 AI 알림",
            body="AI가 상황을 확인하고 있어요.",
            visible_at=clock.current_time,
            related_dose_event_id=44,
            metadata={"status": "awaiting_agent"},
        )
        alert_id = alert.id
        session.commit()

    monkeypatch.setattr(system_main, "SessionLocal", session_factory)

    stop_event = threading.Event()
    worker = threading.Thread(
        target=system_main.agent_worker,
        args=(stop_event,),
        daemon=True,
    )
    worker.start()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with system_main.write_lock:
            with session_factory() as session:
                job = session.get(AgentJob, job_id)
                if job and job.status == FAILED:
                    break
        time.sleep(0.05)

    assert worker.is_alive()
    stop_event.set()
    worker.join(timeout=2)
    assert not worker.is_alive()

    with system_main.write_lock:
        with session_factory() as session:
            job = session.get(AgentJob, job_id)
            assert job is not None
            assert job.status == FAILED
            alert = session.get(Notification, alert_id)
            assert alert is not None
            metadata = json.loads(alert.metadata_json)
            assert metadata["status"] == "agent_error"
            assert metadata["agent_job_id"] == job_id
            notification = (
                session.query(Notification)
                .filter(Notification.notification_type == "agent_error")
                .one()
            )
            assert "payload를 해석하지 못했습니다" in notification.body

    assert "agent_job_payload_invalid job_id=" in caplog.text
