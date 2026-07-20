import asyncio
import json
import logging
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace

import pytest

import system_app.main as system_main
import system_app.services.workers as worker_services
from agent_app.tool_names import CREATE_MEDICATION_SIDE_EFFECT_RECORD
from shared.json_utils import dump_json
from shared.schemas import (
    AgentResponse,
    MissedDoseEventPayload,
    MultiturnChatRequest,
    MutationConfirmationResolutionRequest,
    SlotAdherenceSummary,
)
from shared.time_utils import utc_now
from system_app.models import AgentJob, ChatMessage, MutationConfirmation, Notification
from system_app.services.agent_client import AgentClient, AgentServiceError
from system_app.services.agent_jobs import FAILED, create_agent_job
from system_app.services.mutation_confirmation_service import APPLIED, CANCELLED, EXECUTING
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


def test_mutation_confirmation_resolution_timeout_exceeds_llm_timeout(monkeypatch):
    client = AgentClient(base_url="http://agent.test")
    client.llm_timeout_seconds = 60
    observed: dict[str, float] = {}

    async def fake_request_json(method, path, *, payload=None, timeout=60.0):
        observed["timeout"] = timeout
        return AgentResponse(
            trace_id="trace-resolution",
            agent_name="multiturn_chat_agent",
            prompt_version_id="test",
            decision_type="system_guidance",
            structured_payload={},
            human_summary="resolved",
        ).model_dump(mode="json")

    monkeypatch.setattr(client, "_request_json", fake_request_json)
    request = MutationConfirmationResolutionRequest(
        confirmation_id="confirmation-timeout",
        resolution="confirm",
        action_name="update_medication_dose_event_status",
        arguments={"dose_event_id": 1},
        action_fingerprint="fingerprint-timeout",
        source_event_type="medication_agent",
        original_request=MultiturnChatRequest(
            patient_id="demo-patient",
            event_type="multiturn_chat",
            message="I took my dose.",
            current_time=datetime(2026, 4, 20, 9, 30),
        ),
    )

    response = asyncio.run(client.resolve_mutation_confirmation(request))

    assert response.human_summary == "resolved"
    assert observed["timeout"] == 90.0


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


def test_mutation_worker_preserves_applied_state_when_supervisor_finalization_times_out(monkeypatch):
    session_factory = build_threadsafe_session_factory()
    write_lock = threading.RLock()
    confirmation_id = "confirmation-finalization-timeout"
    with session_factory() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        request_notification = create_system_event_request(
            session,
            "multiturn_chat",
            "I took my lunch dose.",
            clock.current_time,
        )
        card_message = ChatMessage(
            patient_id="demo-patient",
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            content="Confirm the medication update.",
            metadata_json=dump_json(
                {
                    "mutation_confirmation": {
                        "confirmation_id": confirmation_id,
                        "status": EXECUTING,
                        "display": {"title": "dose update", "question": "Apply it?"},
                    }
                }
            ),
            created_at=clock.current_time,
        )
        session.add(card_message)
        session.flush()
        confirmation = MutationConfirmation(
            public_id=confirmation_id,
            patient_id="demo-patient",
            origin_request_notification_id=request_notification.id,
            conversation_id="conversation-timeout",
            origin_trace_id="trace-confirmation",
            origin_agent="medication_agent",
            source_event_type="medication_agent",
            action_type="agent_tool",
            action_name="update_medication_dose_event_status",
            tool_call_id="tool-confirmation",
            arguments_json=dump_json({"dose_event_id": 1}),
            action_fingerprint="fingerprint-timeout",
            target_snapshot_json=dump_json({}),
            target_snapshot_hash="snapshot-timeout",
            display_json=dump_json({"title": "dose update", "question": "Apply it?"}),
            continuation_json=dump_json({}),
            idempotency_key="confirmation-finalization-timeout-key",
            status=EXECUTING,
            chat_message_id=card_message.id,
            execution_started_at=utc_now(),
        )
        session.add(confirmation)
        request_notification_id = request_notification.id
        session.commit()

    class AppliedThenTimeoutClient:
        async def resolve_mutation_confirmation(self, payload):
            with session_factory() as session:
                row = session.query(MutationConfirmation).filter(MutationConfirmation.public_id == confirmation_id).one()
                row.status = APPLIED
                row.result_json = dump_json({"status": "taken"})
                row.resolved_at = utc_now()
                session.commit()
            raise AgentServiceError("finalization timed out", error_type="agent_network_error")

    monkeypatch.setattr(worker_services, "SessionLocal", session_factory)
    worker_services.mutation_confirmation_worker(
        confirmation_id,
        "confirm",
        write_lock,
        AppliedThenTimeoutClient(),
    )

    with session_factory() as session:
        row = session.query(MutationConfirmation).filter(MutationConfirmation.public_id == confirmation_id).one()
        card = session.get(ChatMessage, row.chat_message_id)
        card_metadata = json.loads(card.metadata_json)
        request_notification = session.get(Notification, request_notification_id)
        request_metadata = json.loads(request_notification.metadata_json)
        error_message = session.query(ChatMessage).filter(ChatMessage.category == "error").one()
        error_notification = session.query(Notification).filter(Notification.notification_type == "agent_error").one()
        error_metadata = json.loads(error_notification.metadata_json)

        assert row.status == APPLIED
        assert card_metadata["mutation_confirmation"]["status"] == APPLIED
        assert request_metadata["status"] == APPLIED
        assert request_metadata["result_message"] == worker_services.MUTATION_APPLIED_FINALIZATION_FAILED_MESSAGE
        assert error_message.content == worker_services.MUTATION_APPLIED_FINALIZATION_FAILED_MESSAGE
        assert error_metadata["mutation_status"] == APPLIED
        assert error_metadata["finalization_failed"] is True


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


@pytest.mark.parametrize(
    ("resolution", "resolved_status", "expected_recorded", "expected_fragment"),
    [
        ("confirm", APPLIED, True, "기록했습니다"),
        ("cancel", CANCELLED, False, "기록하지 않았습니다"),
    ],
)
def test_side_effect_confirmation_starts_safety_prompt_after_supervisor_response(
    monkeypatch,
    resolution,
    resolved_status,
    expected_recorded,
    expected_fragment,
):
    session_factory = build_threadsafe_session_factory()
    write_lock = threading.RLock()
    confirmation_id = "confirmation-side-effect-safety-order"
    with session_factory() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        request_notification = create_system_event_request(
            session,
            "multiturn_chat",
            "속이 메스꺼워요",
            clock.current_time,
        )
        card_message = ChatMessage(
            patient_id="demo-patient",
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            content="부작용 평가 결과를 기록할까요?",
            metadata_json=dump_json(
                {
                    "mutation_confirmation": {
                        "confirmation_id": confirmation_id,
                        "status": EXECUTING,
                        "display": {
                            "title": "부작용 평가 기록",
                            "question": "메스꺼움 증상에 대한 부작용 평가 결과를 기록할까요?",
                        },
                    }
                }
            ),
            created_at=clock.current_time,
        )
        session.add(card_message)
        session.flush()
        session.add(
            MutationConfirmation(
                public_id=confirmation_id,
                patient_id="demo-patient",
                origin_request_notification_id=request_notification.id,
                conversation_id="conversation-side-effect-safety-order",
                origin_trace_id="trace-side-effect-safety-order",
                origin_agent="medication_agent",
                source_event_type="medication_agent",
                action_type="agent_tool",
                action_name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
                tool_call_id="tool-side-effect-record",
                arguments_json=dump_json({"symptom_text": "메스꺼움", "suspected": True}),
                action_fingerprint="fingerprint-side-effect-safety-order",
                target_snapshot_json=dump_json({"record": None}),
                target_snapshot_hash="snapshot-side-effect-safety-order",
                display_json=dump_json(
                    {
                        "title": "부작용 평가 기록",
                        "question": "메스꺼움 증상에 대한 부작용 평가 결과를 기록할까요?",
                    }
                ),
                continuation_json=dump_json({}),
                idempotency_key="confirmation-side-effect-safety-order-key",
                status=EXECUTING,
                chat_message_id=card_message.id,
                execution_started_at=utc_now(),
            )
        )
        session.commit()

    final_summary = "부작용 평가 기록을 완료했습니다." if resolved_status == APPLIED else "부작용 평가 기록을 취소했습니다."

    class AppliedSideEffectClient:
        async def resolve_mutation_confirmation(self, payload):
            with session_factory() as session:
                row = session.query(MutationConfirmation).filter(MutationConfirmation.public_id == confirmation_id).one()
                row.status = resolved_status
                row.result_json = dump_json({"success": True})
                row.resolved_at = utc_now()
                session.commit()
            return AgentResponse(
                trace_id="trace-side-effect-safety-final",
                agent_name="multiturn_chat_agent",
                prompt_version_id="multiturn_chat_agent_test",
                decision_type="mutation_resolution",
                structured_payload={
                    "routing_mode": "mutation_resolution_finalization",
                    "mutation_resolution": {"status": resolved_status},
                },
                human_summary=final_summary,
            )

    monkeypatch.setattr(worker_services, "SessionLocal", session_factory)
    worker_services.mutation_confirmation_worker(
        confirmation_id,
        resolution,
        write_lock,
        AppliedSideEffectClient(),
    )

    with session_factory() as session:
        supervisor_message = session.query(ChatMessage).filter(ChatMessage.content == final_summary).one()
        safety_message = session.query(ChatMessage).filter(ChatMessage.category == "side_effect_reminder_safety").one()
        safety_notification = session.query(Notification).filter(Notification.notification_type == "conversation_alert").one()
        safety_metadata = json.loads(safety_notification.metadata_json)

        assert supervisor_message.id < safety_message.id
        assert safety_metadata["category"] == "side_effect_reminder_safety"
        assert safety_metadata["side_effect_recorded"] is expected_recorded
        assert expected_fragment in safety_message.content
        confirmation = session.query(MutationConfirmation).filter(MutationConfirmation.public_id == confirmation_id).one()
        assert safety_metadata["source_chat_message_id"] == confirmation.chat_message_id
