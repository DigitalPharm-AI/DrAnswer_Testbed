import asyncio
import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from agent_app.agents.multiturn_chat import MultiturnChatAgent
from agent_app.async_tasks import (
    DEAD,
    FAILED,
    PENDING,
    async_task_by_request_id,
    async_task_observability_payload,
    async_task_rows,
    async_task_status_counts,
    claim_next_async_task,
    dismiss_dead_async_task,
    enqueue_async_task,
    mark_async_task_failed,
    retry_dead_async_task,
)
from agent_app.async_tasks import (
    RUNNING as TASK_RUNNING,
)
from agent_app.async_worker import _execute_snapshot
from agent_app.models import AgentWorkerHeartbeat
from agent_app.models import Base as AgentBase
from agent_app.providers import RuleBasedProvider
from agent_app.tool_runtime import ToolRuntime
from agent_app.worker_status import (
    WORKER_RUNNING,
    WORKER_STALE,
    WORKER_STOPPED,
    mark_worker_started,
    mark_worker_stopped,
    record_worker_heartbeat,
    record_worker_task_completed,
    worker_status_payload,
)
from shared.time_utils import utc_now
from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncJobResultRequest,
    AgentResponse,
    MissedDoseEventPayload,
    MultiturnChatRequest,
)
from system_app.models import AgentJob, ChatMessage, Notification
from system_app.services.agent_async_callback_service import (
    process_async_chat_result_callback,
    process_async_clinician_alert_callback,
    process_async_job_result_callback,
)
from system_app.services.agent_jobs import DONE, RUNNING, create_agent_job
from system_app.services.clock_service import ensure_clock
from tests.helpers import build_session


def build_agent_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    AgentBase.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def test_agent_async_task_enqueue_deduplicates_request_id():
    with build_agent_session() as session:
        first, created_first = enqueue_async_task(
            session,
            request_id="missed_dose:job:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )
        second, created_second = enqueue_async_task(
            session,
            request_id="missed_dose:job:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )

        assert created_first is True
        assert created_second is False
        assert second.id == first.id


def test_agent_async_task_claim_sets_lock_metadata():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:lock:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )

        claimed = claim_next_async_task(session, worker_id="worker-a", visibility_timeout_seconds=60)

        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status == TASK_RUNNING
        assert claimed.attempts == 1
        assert claimed.locked_by == "worker-a"
        assert claimed.locked_until is not None
        assert claimed.locked_until > utc_now()


def test_agent_async_task_failure_retries_then_dead():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:retry:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
            max_attempts=2,
        )

        first_claim = claim_next_async_task(session, worker_id="worker-a")
        assert first_claim is not None
        failed_task, final_failure = mark_async_task_failed(session, first_claim.id, "first failure")

        assert failed_task is not None
        assert final_failure is False
        assert failed_task.status == PENDING
        assert failed_task.run_after is not None
        assert failed_task.locked_by == ""
        assert failed_task.locked_until is None

        task.run_after = utc_now() - timedelta(seconds=1)
        session.flush()
        second_claim = claim_next_async_task(session, worker_id="worker-a")
        assert second_claim is not None
        dead_task, final_failure = mark_async_task_failed(session, second_claim.id, "second failure")

        assert dead_task is not None
        assert final_failure is True
        assert dead_task.status == DEAD
        assert dead_task.completed_at is not None
        assert dead_task.run_after is None


def test_agent_async_task_stale_lock_is_reclaimed():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:stale:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )
        task.status = TASK_RUNNING
        task.attempts = 1
        task.locked_by = "dead-worker"
        task.locked_until = utc_now() - timedelta(seconds=1)
        session.flush()

        claimed = claim_next_async_task(session, worker_id="worker-b", visibility_timeout_seconds=60)

        assert claimed is not None
        assert claimed.id == task.id
        assert claimed.status == TASK_RUNNING
        assert claimed.attempts == 2
        assert claimed.locked_by == "worker-b"


def test_agent_async_task_status_counts_groups_by_status():
    with build_agent_session() as session:
        enqueue_async_task(
            session,
            request_id="missed_dose:status:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:status:2",
            task_type="missed_dose",
            payload={"dose_event_id": 2},
        )
        task.status = DEAD
        session.flush()

        counts = async_task_status_counts(session)

        assert counts[PENDING] == 1
        assert counts[DEAD] == 1


def test_agent_async_task_observability_payload_hides_payload_values():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:observability:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1, "phr_patient_key": "secret-phr-key"},
            callback_context={"job_id": 77},
        )
        task.status = DEAD
        task.last_error = "callback failed"
        task.locked_by = "worker-a"
        session.flush()

        rows = async_task_rows(session, status=DEAD)
        fetched = async_task_by_request_id(session, "missed_dose:observability:1")
        payload = async_task_observability_payload(task)

        assert rows == [task]
        assert fetched == task
        assert payload["request_id"] == "missed_dose:observability:1"
        assert payload["status"] == DEAD
        assert payload["last_error"] == "callback failed"
        assert payload["locked_by"] == "worker-a"
        assert payload["age_seconds"] >= 0
        assert payload["runtime_seconds"] is None
        assert payload["next_retry_in_seconds"] is not None
        assert payload["is_locked"] is False
        assert payload["is_retry_due"] is True
        assert payload["payload_keys"] == ["dose_event_id", "phr_patient_key"]
        assert payload["payload_parse_error"] is False
        assert payload["callback_context"] == {"job_id": 77}
        assert "secret-phr-key" not in json.dumps(payload, ensure_ascii=False)


def test_agent_async_task_observability_payload_uses_completed_runtime():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:observability:completed-runtime",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )
        task.status = DEAD
        task.started_at = utc_now() - timedelta(days=1)
        task.completed_at = task.started_at + timedelta(seconds=7)
        session.flush()

        payload = async_task_observability_payload(task)

        assert payload["runtime_seconds"] == 7


def test_agent_async_task_observability_payload_survives_malformed_payload_json():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:observability:bad-json",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
        )
        task.payload_json = "{not-valid-json"
        session.flush()

        payload = async_task_observability_payload(task)

        assert payload["request_id"] == "missed_dose:observability:bad-json"
        assert payload["payload_keys"] == []
        assert payload["payload_parse_error"] is True


def test_dead_async_task_operator_actions_retry_and_dismiss():
    with build_agent_session() as session:
        retry_task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:operator:retry",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
            max_attempts=1,
        )
        retry_task.status = DEAD
        retry_task.attempts = 1
        retry_task.started_at = utc_now() - timedelta(seconds=10)
        retry_task.completed_at = utc_now()
        retry_task.locked_by = "worker-old"
        retry_task.locked_until = utc_now() + timedelta(seconds=30)
        retry_task.last_error = "token=secret-value"
        session.flush()

        retried = retry_dead_async_task(session, retry_task, reason="operator checked token=secret-value")

        assert retried.status == PENDING
        assert retried.attempts == 0
        assert retried.started_at is None
        assert retried.completed_at is None
        assert retried.locked_by == ""
        assert retried.locked_until is None
        assert retried.run_after is not None
        assert "operator_retry_requested" in retried.last_error
        assert "secret-value" not in retried.last_error

        dismiss_task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:operator:dismiss",
            task_type="missed_dose",
            payload={"dose_event_id": 2},
            max_attempts=1,
        )
        dismiss_task.status = DEAD
        dismiss_task.attempts = 1
        dismiss_task.locked_by = "worker-old"
        dismiss_task.locked_until = utc_now() + timedelta(seconds=30)
        session.flush()

        dismissed = dismiss_dead_async_task(session, dismiss_task, reason="duplicate incident")

        assert dismissed.status == FAILED
        assert dismissed.completed_at is not None
        assert dismissed.locked_by == ""
        assert dismissed.locked_until is None
        assert dismissed.run_after is None
        assert dismissed.last_error == "operator_dismissed: duplicate incident"


def test_agent_worker_heartbeat_status_tracks_running_stopped_and_stale(monkeypatch):
    with build_agent_session() as session:
        mark_worker_started(session, "worker-a")
        record_worker_heartbeat(
            session,
            "worker-a",
            current_task_request_id="missed_dose:job:1",
            current_task_type="missed_dose",
        )
        record_worker_heartbeat(
            session,
            "worker-private-error",
            last_error="pytest private worker error peanut allergy token=secret-value",
        )
        record_worker_task_completed(session, "worker-a")
        running_status = worker_status_payload(session)[0]
        private_error_status = next(item for item in worker_status_payload(session) if item["worker_id"] == "worker-private-error")

        assert running_status["worker_id"] == "worker-a"
        assert running_status["status"] == WORKER_RUNNING
        assert running_status["current_task_request_id"] == ""
        assert running_status["current_task_type"] == ""
        assert running_status["processed_count"] == 1
        assert private_error_status["last_error"].startswith("clinical text redacted")
        assert "pytest private worker error" not in private_error_status["last_error"]
        assert "secret-value" not in private_error_status["last_error"]

        mark_worker_stopped(session, "worker-a")
        stopped_status = worker_status_payload(session)[0]
        assert stopped_status["status"] == WORKER_STOPPED
        assert stopped_status["stopped_at"] is not None

        monkeypatch.setenv("AGENT_WORKER_STALE_AFTER_SECONDS", "1")
        from shared.settings import get_settings

        get_settings.cache_clear()
        try:
            mark_worker_started(session, "worker-b")
            row = session.query(AgentWorkerHeartbeat).filter(AgentWorkerHeartbeat.worker_id == "worker-b").one()
            row.heartbeat_at = utc_now() - timedelta(seconds=5)
            session.flush()
            stale_status = next(item for item in worker_status_payload(session) if item["worker_id"] == "worker-b")
        finally:
            get_settings.cache_clear()

        assert stale_status["status"] == WORKER_STALE
        assert stale_status["stored_status"] == WORKER_RUNNING


def test_multiturn_side_effect_request_returns_async_continuation_ack():
    agent = MultiturnChatAgent(RuleBasedProvider(), ToolRuntime(None))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="속이 메스꺼운데 약 때문일까?",
        current_time=datetime(2026, 4, 20, 9, 30),
        context={},
    )

    response = asyncio.run(agent.run("trace-chat-async", request.model_dump(mode="json")))

    assert response.decision_type == "async_continuation_requested"
    assert response.human_summary
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert response.structured_payload["specialist_tool_calls"][0]["name"] == "get_medication_side_effect_assessment"
    assert response.structured_payload["async_continuation_required"] is True
    assert response.structured_payload["async_continuation_type"] == "side_effect_assessment"
    assert response.structured_payload["tool_calls"][0]["name"] == "get_medication_side_effect_assessment"


def test_multiturn_policy_request_returns_async_continuation_ack():
    agent = MultiturnChatAgent(RuleBasedProvider(), ToolRuntime(None))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="아침 알림을 2번 10분 간격으로 바꿔줘",
        current_time=datetime(2026, 4, 20, 9, 30),
        context={},
    )

    response = asyncio.run(agent.run("trace-policy-async", request.model_dump(mode="json")))

    assert response.decision_type == "async_continuation_requested"
    assert response.human_summary == "알림 정책 변경 후보를 만들고 있어요. 준비되면 확인할 수 있게 보여드릴게요."
    assert response.structured_payload["async_continuation_type"] == "policy_change_request"
    assert response.structured_payload["policy_confirmation_required"] is True
    assert response.structured_payload["tool_calls"][0]["name"] == "propose_notification_policy"


def test_async_missed_dose_job_result_persists_once_by_idempotency_key():
    with build_session() as session:
        payload = MissedDoseEventPayload(
            patient_id="demo-patient",
            dose_event_id=33,
            medication_name="영양제",
            slot_label="아침 08:00",
            scheduled_for=datetime(2026, 4, 20, 8, 0),
            detected_at=datetime(2026, 4, 20, 9, 30),
        )
        job = create_agent_job(session, "missed_dose", payload)
        job.status = RUNNING
        response = AgentResponse(
            trace_id="trace-missed-async",
            agent_name="missed_dose_coach",
            prompt_version_id="v1",
            decision_type="missed_dose_assessment",
            structured_payload={},
            human_summary="복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
            requires_conversation_alert=True,
        )
        callback = AgentAsyncJobResultRequest(
            request_id=f"missed_dose:job:{job.id}",
            task_type="missed_dose",
            job_id=job.id,
            related_dose_event_id=33,
            response=response,
            idempotency_key="missed-dose-result-once",
        )

        first = process_async_job_result_callback(session, callback)
        second = process_async_job_result_callback(session, callback)

        assert first["status"] == "ok"
        assert second["status"] == "duplicate"
        assert session.get(AgentJob, job.id).status == DONE
        assert session.query(ChatMessage).filter(ChatMessage.category == "missed_dose").count() == 1


def test_async_chat_result_creates_ae_pro_ctcae_chat_prompt():
    with build_session() as session:
        clock = ensure_clock(session)
        notification = Notification(
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata_json=json.dumps({"request_message": "속이 메스꺼워요", "status": "sent"}, ensure_ascii=False),
        )
        session.add(notification)
        session.flush()
        response = AgentResponse(
            trace_id="trace-ae-async",
            agent_name="side_effect_triage_agent",
            prompt_version_id="v1",
            decision_type="side_effect_assessment",
            structured_payload={
                "ae_pro_ctcae": {
                    "input_symptom": "메스꺼움",
                    "matched": True,
                    "match_type": "exact",
                    "matched_symptom_term": "Nausea",
                    "matched_korean_symptom_name": "메스꺼움",
                    "similarity": 1.0,
                    "threshold": 0.56,
                    "sheet_name": "PRO-CTCAE",
                    "questions": [
                        {
                            "symptom_term": "Nausea",
                            "korean_symptom_name": "메스꺼움",
                            "item_code": "PROCTCAE_NAUSEA",
                            "question": "지난 7일 동안 메스꺼움이 있었나요?",
                            "sheet_name": "PRO-CTCAE",
                        }
                    ],
                }
            },
            human_summary=(
                "말씀하신 메스꺼움은 복용약과 관련 가능성이 있어 확인 문항을 준비했습니다. "
                "아래 문항에 답해주세요."
            ),
        )
        callback = AgentAsyncChatResultRequest(
            request_id="chat_continuation:notification:1",
            notification_id=notification.id,
            event_type="multiturn_chat",
            message="속이 메스꺼워요",
            response=response,
            idempotency_key="chat-ae-result-once",
        )

        result = process_async_chat_result_callback(session, callback)

        assert result["status"] == "ok"
        message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.role == "assistant").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == "말씀하신 메스꺼움은 복용약과 관련 가능성이 있어 확인 문항을 준비했습니다. 아래 문항에 답해주세요."
        assert metadata["ae_pro_ctcae"]["matched"] is True
        assert metadata["ae_pro_ctcae"]["questions"][0]["item_code"] == "PROCTCAE_NAUSEA"


def test_async_chat_result_preserves_llm_food_selection_message():
    with build_session() as session:
        clock = ensure_clock(session)
        notification = Notification(
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata_json=json.dumps({"request_message": "삶은 계란 먹었어", "status": "sent"}, ensure_ascii=False),
        )
        session.add(notification)
        session.flush()
        response = AgentResponse(
            trace_id="trace-food-async",
            agent_name="system_event_agent",
            prompt_version_id="v1",
            decision_type="tool_call",
            structured_payload={
                "food_searches": [
                    {
                        "query": "삶은 계란",
                        "candidates": [
                            {
                                "food_ref_id": "egg-boiled",
                                "food_name": "삶은 달걀",
                                "serving_size": 50,
                                "nutrients": {"calories": 70, "protein": 6},
                            }
                        ],
                    }
                ]
            },
            human_summary="음식 후보를 찾았습니다. 아래 카드에서 선택해주세요.",
        )
        callback = AgentAsyncChatResultRequest(
            request_id="chat_continuation:notification:food",
            notification_id=notification.id,
            event_type="multiturn_chat",
            message="삶은 계란 먹었어",
            response=response,
            idempotency_key="chat-food-result-once",
        )

        result = process_async_chat_result_callback(session, callback)

        assert result["status"] == "ok"
        message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.role == "assistant").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == "음식 후보를 찾았습니다. 아래 카드에서 선택해주세요."
        assert metadata["food_selection"]["candidates"][0]["food_name"] == "삶은 달걀"

def test_async_chat_result_skips_food_selection_after_nutrition_write():
    with build_session() as session:
        clock = ensure_clock(session)
        notification = Notification(
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냈습니다.",
            visible_at=clock.current_time,
            metadata_json=json.dumps({"request_message": "마라탕을 영양돌솥밥으로 바꿔줘", "status": "sent"}, ensure_ascii=False),
        )
        session.add(notification)
        session.flush()
        response = AgentResponse(
            trace_id="trace-food-update-async",
            agent_name="system_event_agent",
            prompt_version_id="v1",
            decision_type="tool_call",
            structured_payload={
                "food_searches": [
                    {
                        "query": "영양돌솥밥",
                        "candidates": [
                            {
                                "food_ref_id": "rice-pot",
                                "food_name": "영양돌솥밥",
                                "serving_size": 350,
                                "nutrients": {"calories": 644, "protein": 12},
                            }
                        ],
                    }
                ],
                "nutrition_food_update_result": {"success": True, "food": {"food_name": "영양돌솥밥"}},
                "tool_results": [
                    {"tool_name": "search_nutrition_food_candidates", "status": "success", "response": {"success": True}},
                    {"tool_name": "update_nutrition_food_record", "status": "success", "response": {"success": True}},
                ],
                "tools_executed": True,
            },
            human_summary="점심 마라탕을 영양돌솥밥으로 교체했어요.",
        )
        callback = AgentAsyncChatResultRequest(
            request_id="chat_continuation:notification:food-update",
            notification_id=notification.id,
            event_type="multiturn_chat",
            message="마라탕을 영양돌솥밥으로 바꿔줘",
            response=response,
            idempotency_key="chat-food-update-result-once",
        )

        result = process_async_chat_result_callback(session, callback)

        assert result["status"] == "ok"
        message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.role == "assistant").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == "점심 마라탕을 영양돌솥밥으로 교체했어요."
        assert "food_selection" not in metadata


def test_async_chat_result_preserves_diet_recommendation_cards():
    with build_session() as session:
        clock = ensure_clock(session)
        notification = Notification(
            notification_type="system_policy_request",
            title="에이전트 대화 전송",
            body="에이전트에게 메시지를 보냅니다.",
            visible_at=clock.current_time,
            metadata_json=json.dumps({"request_message": "신장 건강에 좋은 식사 추천해줘", "status": "sent"}, ensure_ascii=False),
        )
        session.add(notification)
        session.flush()
        response = AgentResponse(
            trace_id="trace-diet-recommendation-async",
            agent_name="system_event_agent",
            prompt_version_id="v1",
            decision_type="tool_call",
            structured_payload={
                "diet_recommendations": [
                    {
                        "food_ref_id": "tofu-salad",
                        "food_name": "두부 샐러드",
                        "category": "샐러드",
                        "serving_size": 180,
                        "nutrients": {"energy": {"value": 210, "unit": "kcal"}},
                    }
                ],
                "constraints_applied": {"나트륨": "low"},
                "blocked_count": 1,
                "total_candidates": 8,
            },
            human_summary="신장 건강을 위한 추천 후보를 아래에 준비했어요.",
        )
        callback = AgentAsyncChatResultRequest(
            request_id="chat_continuation:notification:diet",
            notification_id=notification.id,
            event_type="multiturn_chat",
            message="신장 건강에 좋은 식사 추천해줘",
            response=response,
            idempotency_key="chat-diet-result-once",
        )

        result = process_async_chat_result_callback(session, callback)

        assert result["status"] == "ok"
        message = session.query(ChatMessage).filter(ChatMessage.category == "multiturn_chat", ChatMessage.role == "assistant").one()
        metadata = json.loads(message.metadata_json)
        assert message.content == "신장 건강을 위한 추천 후보를 아래에 준비했어요."
        assert metadata["diet_recommendations"]["recommendations"][0]["food_name"] == "두부 샐러드"
        assert metadata["diet_recommendations"]["constraints_applied"] == {"나트륨": "low"}


def test_async_chat_worker_executes_required_continuation_before_callback(monkeypatch):
    class ContinuationOrchestrator:
        def __init__(self) -> None:
            self.payloads = []

        async def invoke(self, _mode, payload):
            self.payloads.append(payload)
            if len(self.payloads) == 1:
                return AgentResponse(
                    trace_id="trace-first",
                    agent_name="system_event_agent",
                    prompt_version_id="v1",
                    decision_type="async_continuation_requested",
                    structured_payload={
                        "tool_calls": [{"name": "get_medication_side_effect_assessment", "arguments": {"symptom_text": "메스꺼움"}}],
                        "tool_results": [],
                        "async_continuation_required": True,
                        "async_continuation_type": "side_effect_lookup",
                    },
                    human_summary="증상 내용을 확인해서 문항을 준비할게요.",
                )
            return AgentResponse(
                trace_id="trace-final",
                agent_name="side_effect_triage_agent",
                prompt_version_id="v1",
                decision_type="side_effect_assessment",
                structured_payload={"tool_results": [{"tool_name": "get_pro_ctcae_questionnaire", "status": "success"}]},
                human_summary="의료진에게 알려야 할 증상인지 확인하기 위해 식사와 수분 상태를 같이 확인해 주세요.",
            )

    captured = {}

    async def fake_post_chat_result(snapshot, response):
        captured["snapshot"] = snapshot
        captured["response"] = response

    monkeypatch.setattr("agent_app.async_worker._post_chat_result", fake_post_chat_result)
    orchestrator = ContinuationOrchestrator()

    asyncio.run(
        _execute_snapshot(
            {
                "id": 1,
                "request_id": "chat_continuation:conversation:system-event-test",
                "task_type": "chat_continuation",
                "payload": {
                    "patient_id": "demo-patient",
                    "event_type": "multiturn_chat",
                    "message": "메스꺼움이 있어요",
                    "current_time": "2026-04-20T09:35:00",
                    "context": {},
                },
                "callback_context": {"app_base_url": "http://system", "notification_id": 1},
            },
            orchestrator,
        )
    )

    assert len(orchestrator.payloads) == 2
    assert orchestrator.payloads[1]["context"]["execute_async_continuation"] is True
    assert orchestrator.payloads[1]["context"]["async_tool_calls"][0]["name"] == "get_medication_side_effect_assessment"
    assert captured["response"].decision_type == "side_effect_assessment"
    assert "의료진" in captured["response"].human_summary


def test_async_clinician_alert_callback_creates_internal_only_stub_once():
    with build_session() as session:
        ensure_clock(session)
        callback = AgentAsyncClinicianAlertRequest(
            request_id="clinician_alert:event:77:D",
            idempotency_key="clinician-alert-once",
            title="의료진 확인 필요",
            message="미복용 패턴을 의료진 확인 대상으로 기록했습니다.",
            related_dose_event_id=77,
            pattern_code="D",
            pattern_label="장기 연속 미복용",
            reason="현재 시간대에서 3회 연속 미복용이 확인되었습니다.",
            priority="high",
            metadata={"external_delivery": False},
            streak_metrics={"current_consecutive_missed_days": 3},
        )

        first = process_async_clinician_alert_callback(session, callback)
        second = process_async_clinician_alert_callback(session, callback)

        assert first["status"] == "ok"
        assert second["status"] == "duplicate"
        assert session.query(Notification).filter(Notification.notification_type == "clinician_escalation").count() == 1
        notification = session.get(Notification, first["notification_id"])
        metadata = json.loads(notification.metadata_json)
        assert notification.title == "의료진 확인 필요"
        assert notification.body == "미복용 패턴을 의료진 확인 대상으로 기록했습니다."
        assert notification.related_dose_event_id == 77
        assert metadata["category"] == "clinician_escalation"
        assert metadata["delivery_channel"] == "internal_only"
        assert metadata["status"] == "stubbed"
        assert metadata["source"] == "agent_async_clinician_alert"
        assert metadata["pattern_code"] == "D"
        assert metadata["pattern_label"] == "장기 연속 미복용"
        assert metadata["priority"] == "high"
        assert metadata["streak_metrics"]["current_consecutive_missed_days"] == 3


def test_async_clinician_alert_callback_dedupes_same_dose_event_and_pattern():
    with build_session() as session:
        ensure_clock(session)
        first = AgentAsyncClinicianAlertRequest(
            request_id="clinician_alert:first",
            idempotency_key="clinician-alert-first",
            related_dose_event_id=88,
            pattern_code="E",
            pattern_label="전반적 저조",
        )
        replay = first.model_copy(
            update={
                "request_id": "clinician_alert:replay",
                "idempotency_key": "clinician-alert-replay",
            }
        )

        created = process_async_clinician_alert_callback(session, first)
        duplicate = process_async_clinician_alert_callback(session, replay)

        assert created["status"] == "ok"
        assert duplicate["status"] == "duplicate"
        assert duplicate["notification_id"] == created["notification_id"]
        assert session.query(Notification).filter(Notification.notification_type == "clinician_escalation").count() == 1
