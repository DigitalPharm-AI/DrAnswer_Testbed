import asyncio
import json
import logging
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import system_app.main as system_main
import system_app.services.workers as worker_services
from shared.schemas import AgentResponse, MissedDoseEventPayload, SlotAdherenceSummary
from system_app.models import AgentJob, ChatMessage, Notification
from system_app.services.agent_client import AgentServiceError
from system_app.services.agent_jobs import FAILED, create_agent_job
from system_app.services.simulation import (
    create_medication_plan,
    create_notification,
    ensure_base_data,
    ensure_clock,
    handle_system_event,
    run_manual_pattern_analysis,
)
from system_app.services.system_request_service import create_system_event_request
from tests.helpers import build_session, build_threadsafe_session_factory


class FailingAgentClient:
    def __init__(self, error: AgentServiceError) -> None:
        self.error = error

    async def send_multiturn_chat(self, payload):
        raise self.error

    async def send_daily_pattern_async(self, payload):
        raise self.error

    async def send_missed_dose_async(self, payload):
        raise self.error

    async def send_chat_continuation_async(self, payload):
        raise self.error


class LockCheckingSystemEventAgentClient:
    def __init__(self, write_lock: threading.Lock) -> None:
        self.write_lock = write_lock
        self.called_without_write_lock = False

    async def send_multiturn_chat(self, payload):
        acquired = self.write_lock.acquire(blocking=False)
        if acquired:
            self.called_without_write_lock = True
            self.write_lock.release()
        return AgentResponse(
            trace_id="trace-system-worker",
            agent_name="multiturn_chat_agent",
            prompt_version_id="multiturn_chat_agent_v11",
            decision_type="side_effect_assessment",
            structured_payload={
                "tool_call": {
                    "name": "get_medication_side_effect_assessment",
                    "arguments": {"symptom_text": payload.message},
                },
                "tools_executed": True,
            },
            human_summary="증상 질문에 답변했습니다.",
            requires_conversation_alert=False,
        )


class LockCheckingAsyncSystemEventAgentClient:
    def __init__(self, write_lock: threading.Lock) -> None:
        self.write_lock = write_lock
        self.called_without_write_lock = False
        self.payload_message = ""

    async def send_chat_continuation_async(self, payload):
        acquired = self.write_lock.acquire(blocking=False)
        if acquired:
            self.called_without_write_lock = True
            self.write_lock.release()
        self.payload_message = payload.message
        return SimpleNamespace(request_id="chat_continuation:conversation:system-event-test", task_type="chat_continuation")


def test_handle_system_event_creates_agent_error_notification_and_chat():
    with build_session() as session:
        ensure_base_data(session)
        client = FailingAgentClient(
            AgentServiceError(
                "policy_planner 모델 호출에 실패했습니다.",
                error_type="provider_request_failed",
                trace_id="trace-system",
                agent_name="policy_planner",
                decision_type="system_guidance",
            )
        )

        asyncio.run(handle_system_event(session, client, "multiturn_chat", "현재 상태를 어떻게 판단하니?"))

        notification = session.query(Notification).filter(Notification.notification_type == "agent_error").one()
        assert "AI가 대화를 처리하지 못했습니다." in notification.body
        error_message = session.query(ChatMessage).filter(ChatMessage.category == "error").one()
        assert "AI가 대화를 처리하지 못했습니다." in error_message.content


def test_system_event_worker_releases_write_lock_while_calling_agent(monkeypatch):
    session_factory = build_threadsafe_session_factory()
    write_lock = threading.Lock()
    client = LockCheckingSystemEventAgentClient(write_lock)
    with session_factory() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        notification = create_system_event_request(session, "multiturn_chat", "속이 매스꺼운데 약때문일까?", clock.current_time)
        notification_id = notification.id
        session.commit()

    monkeypatch.setattr(worker_services, "SessionLocal", session_factory)

    worker_services.system_event_worker("multiturn_chat", "속이 매스꺼운데 약때문일까?", notification_id, write_lock, client)

    with session_factory() as session:
        notification = session.get(Notification, notification_id)
        metadata = json.loads(notification.metadata_json)
        assistant_message = session.query(ChatMessage).filter(ChatMessage.role == "assistant", ChatMessage.category == "multiturn_chat").one()

        assert client.called_without_write_lock is True
        assert metadata["status"] == "answered"
        assert assistant_message.content == "증상 질문에 답변했습니다."
        assert session.query(Notification).filter(Notification.notification_type == "agent_error").count() == 0


def test_system_event_worker_submits_chat_to_agent_async_without_write_lock(monkeypatch):
    session_factory = build_threadsafe_session_factory()
    write_lock = threading.Lock()
    client = LockCheckingAsyncSystemEventAgentClient(write_lock)
    with session_factory() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        notification = create_system_event_request(session, "multiturn_chat", "오늘 점심과 복약을 같이 봐줘", clock.current_time)
        notification_id = notification.id
        session.commit()

    monkeypatch.setattr(worker_services, "SessionLocal", session_factory)

    worker_services.system_event_worker("multiturn_chat", "오늘 점심과 복약을 같이 봐줘", notification_id, write_lock, client)

    with session_factory() as session:
        notification = session.get(Notification, notification_id)
        metadata = json.loads(notification.metadata_json)

        assert client.called_without_write_lock is True
        assert client.payload_message == "오늘 점심과 복약을 같이 봐줘"
        assert notification.title == "에이전트 답변 대기"
        assert metadata["status"] == "awaiting_agent"
        assert metadata["async_continuation_status"] == "pending"
        assert metadata["agent_async_request_id"] == "chat_continuation:conversation:system-event-test"
        assert session.query(ChatMessage).filter(ChatMessage.role == "assistant", ChatMessage.category == "multiturn_chat").count() == 0


def test_run_manual_pattern_analysis_enqueues_daily_pattern_job_without_waiting_for_agent():
    with build_session() as session:
        ensure_base_data(session)
        create_medication_plan(
            session,
            medication_name="검증용 혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00,21:00",
            instructions="테스트",
        )
        client = FailingAgentClient(
            AgentServiceError(
                "pattern_analyzer 모델 호출에 실패했습니다.",
                error_type="provider_request_failed",
                trace_id="trace-manual",
                agent_name="pattern_analyzer",
                decision_type="pattern_analysis",
            )
        )

        result = asyncio.run(run_manual_pattern_analysis(session, client))

        assert result is True
        job = session.query(AgentJob).filter(AgentJob.job_type == "daily_pattern").one()
        assert job.status == "pending"
        assert session.query(Notification).filter(Notification.notification_type == "agent_error").count() == 0
        assert session.query(ChatMessage).filter(ChatMessage.category == "error").count() == 0


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
            trace_id="trace-background",
            agent_name="missed_dose_coach",
            decision_type="missed_dose_assessment",
        )
    )
    payload = MissedDoseEventPayload(
        patient_id="demo-patient",
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
    worker = threading.Thread(target=system_main.agent_worker, args=(stop_event,), daemon=True)
    worker.start()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with session_factory() as session:
            job = session.get(AgentJob, job_id)
            if job and job.status == FAILED:
                break
        time.sleep(0.05)

    with session_factory() as session:
        job = session.get(AgentJob, job_id)
        assert job is not None
        assert job.status == FAILED
    assert worker.is_alive()

    stop_event.set()
    worker.join(timeout=2)
    assert not worker.is_alive()

    with session_factory() as session:
        clock = ensure_clock(session)
        assert clock.is_running is True
        assert clock.speed_multiplier == 10
        notification = session.query(Notification).filter(Notification.notification_type == "agent_error").one()
        assert "백그라운드 AI 작업을 처리하지 못했습니다." in notification.body
        alert = session.get(Notification, alert_id)
        assert alert is not None
        assert "AI가 미복용 상황을 처리하지 못했습니다." in alert.body
        metadata = json.loads(alert.metadata_json)
        assert metadata["status"] == "agent_error"
        assert metadata["agent_job_id"] == job_id



def test_create_agent_job_deduplicates_active_missed_dose_job():
    with build_session() as session:
        payload = MissedDoseEventPayload(
            patient_id="demo-patient",
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


def test_agent_worker_marks_invalid_payload_failed_without_stopping(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error")
    session_factory = build_threadsafe_session_factory()
    payload = MissedDoseEventPayload(
        patient_id="demo-patient",
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
    worker = threading.Thread(target=system_main.agent_worker, args=(stop_event,), daemon=True)
    worker.start()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with session_factory() as session:
            job = session.get(AgentJob, job_id)
            if job and job.status == FAILED:
                break
        time.sleep(0.05)

    with session_factory() as session:
        job = session.get(AgentJob, job_id)
        assert job is not None
        assert job.status == FAILED
        alert = session.get(Notification, alert_id)
        assert alert is not None
        metadata = json.loads(alert.metadata_json)
        assert metadata["status"] == "agent_error"
        assert metadata["agent_job_id"] == job_id
        notification = session.query(Notification).filter(Notification.notification_type == "agent_error").one()
        assert "payload를 해석하지 못했습니다" in notification.body

    assert worker.is_alive()
    assert "agent_job_payload_invalid job_id=" in caplog.text

    stop_event.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
