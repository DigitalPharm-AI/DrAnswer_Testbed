import asyncio
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import sessionmaker

from agent_app.agents.multiturn_chat import MultiturnChatAgent
from agent_app.errors import AgentExecutionError
from agent_app.jobs import worker as async_worker
from agent_app.jobs.status import (
    WORKER_RUNNING,
    WORKER_STALE,
    WORKER_STOPPED,
    mark_worker_started,
    mark_worker_stopped,
    record_worker_heartbeat,
    record_worker_task_completed,
    worker_status_payload,
)
from agent_app.jobs.tasks import (
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
    mark_async_task_done,
    mark_async_task_failed,
    mark_async_task_terminal_failure,
    retry_dead_async_task,
    stage_async_task_callback_delivery,
)
from agent_app.jobs.tasks import (
    RUNNING as TASK_RUNNING,
)
from agent_app.persistence.models import AgentWorkerHeartbeat
from agent_app.providers.deterministic_test import DeterministicTestProvider
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentCallbackContext,
    MultiturnChatRequest,
)
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import (
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    Notification,
)
from system_app.services.agent_async_callback_service import (
    process_async_clinician_alert_callback,
)
from system_app.services.clock_service import ensure_clock
from tests.helpers import build_agent_engine, build_session

TEST_PATIENT_ID = get_settings().patient_id


def build_agent_session():
    engine, _cleanup = build_agent_engine("agent_async_callbacks")
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)()


def _seed_dose_event(
    session,
    *,
    event_id: int,
    status: str = "scheduled",
) -> DoseEvent:
    plan, schedule = _seed_dose_parent(session)
    event = DoseEvent(
        id=event_id,
        patient_id=TEST_PATIENT_ID,
        plan_id=plan.id,
        schedule_id=schedule.id,
        medication_name="test medication",
        slot_label="morning 08:00",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        status=status,
    )
    session.add(event)
    session.flush()
    return event


def _seed_dose_parent(session) -> tuple[MedicationPlan, DoseSchedule]:
    plan = MedicationPlan(
        patient_id=TEST_PATIENT_ID,
        medication_name="test medication",
        start_date=datetime(2026, 4, 20).date(),
        end_date=datetime(2026, 4, 20).date(),
        active=True,
    )
    session.add(plan)
    session.flush()
    schedule = DoseSchedule(
        plan_id=plan.id,
        slot_label="morning 08:00",
        scheduled_time="08:00",
    )
    session.add(schedule)
    session.flush()
    return plan, schedule


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


def test_agent_async_task_uncertain_provider_timeout_is_terminal():
    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id="missed_dose:timeout:1",
            task_type="missed_dose",
            payload={"dose_event_id": 1},
            max_attempts=3,
        )
        claimed = claim_next_async_task(
            session,
            worker_id="worker-a",
            visibility_timeout_seconds=60,
        )
        assert claimed is not None

        failed = mark_async_task_terminal_failure(
            session,
            task.id,
            "llm_generation_timeout:0.1s",
        )

        assert failed is not None
        assert failed.status == DEAD
        assert failed.attempts == 1
        assert failed.completed_at is not None
        assert failed.run_after is None
        assert failed.locked_by == ""
        assert failed.locked_until is None


def test_worker_only_callbacks_missed_dose_failure_with_safe_public_error():
    missed_dose = async_worker._failure_callback_envelope(
        {
            "request_id": "req_0000000012345678",
            "task_type": "missed_dose",
        },
        RuntimeError("provider secret diagnostic"),
    )
    daily_pattern = async_worker._failure_callback_envelope(
        {
            "request_id": "req_0000000087654321",
            "task_type": "daily_pattern_analysis",
        },
        RuntimeError("provider secret diagnostic"),
    )

    assert missed_dose is not None
    path, payload, bearer = missed_dose
    assert path == "/api/agent/async/missed-dose-results"
    assert bearer is True
    assert payload == {
        "request_id": "req_0000000012345678",
        "status": "failed",
        "result": None,
        "error": {
            "code": "AI_PROCESSING_ERROR",
            "message": (
                "An internal AI Server processing error occurred."
            ),
            "retryable": True,
        },
    }
    assert "provider secret diagnostic" not in json.dumps(payload)
    assert daily_pattern is None


def test_worker_callback_exposes_specific_safe_llm_error():
    callback = async_worker._failure_callback_envelope(
        {
            "request_id": "req_0000000012345678",
            "task_type": "missed_dose",
        },
        AgentExecutionError(
            "provider secret diagnostic",
            error_type="llm_output_parse_failed",
            trace_id="trace-internal",
            agent_name="missed-dose-agent",
            decision_type="missed-dose",
        ),
    )

    assert callback is not None
    _path, payload, _bearer = callback
    assert payload["request_id"] == "req_0000000012345678"
    assert payload["error"] == {
        "code": "LLM_OUTPUT_PARSE_FAILED",
        "message": (
            "The LLM response could not be parsed as the required JSON "
            "object."
        ),
        "retryable": True,
    }
    assert "provider secret diagnostic" not in json.dumps(payload)
    assert "trace-internal" not in json.dumps(payload)


def test_terminal_failure_callback_is_retried_from_persisted_outbox_until_ack(
    monkeypatch,
):
    sent_payloads: list[dict] = []
    callback_payload = {
        "request_id": "missed_dose:failure-callback:1",
        "status": "failed",
        "result": None,
        "error": {
            "code": "AI_PROCESSING_ERROR",
            "message": "provider exhausted",
            "retryable": True,
        },
    }

    with build_agent_session() as session:
        task, _created = enqueue_async_task(
            session,
            request_id=callback_payload["request_id"],
            task_type="missed_dose",
            payload={"dose_event_id": "dose_test"},
            max_attempts=2,
        )
        claimed = claim_next_async_task(session, worker_id="worker-a")
        assert claimed is not None
        mark_async_task_terminal_failure(
            session,
            task.id,
            "provider exhausted",
        )
        staged = stage_async_task_callback_delivery(
            session,
            task.id,
            callback_payload=callback_payload,
            callback_path="/api/agent/async/missed-dose-results",
            callback_bearer=True,
            processing_error="provider exhausted",
        )
        assert staged is not None
        assert staged.status == PENDING
        assert staged.attempts == 0

        callback_claim = claim_next_async_task(
            session,
            worker_id="callback-worker",
        )
        assert callback_claim is not None
        first_snapshot = async_worker._snapshot_task(callback_claim)

        async def fail_callback(_context, _path, payload, **_kwargs):
            sent_payloads.append(payload)
            raise RuntimeError("callback ACK lost")

        monkeypatch.setattr(
            async_worker,
            "_post_callback",
            fail_callback,
        )
        with pytest.raises(RuntimeError, match="ACK lost"):
            asyncio.run(
                async_worker._execute_snapshot(
                    first_snapshot,
                    object(),
                )
            )
        retrying, final_failure = mark_async_task_failed(
            session,
            task.id,
            "callback ACK lost",
        )
        assert retrying is not None
        assert final_failure is False
        assert retrying.status == PENDING
        retrying.run_after = utc_now() - timedelta(seconds=1)
        session.flush()

        second_claim = claim_next_async_task(
            session,
            worker_id="callback-worker",
        )
        assert second_claim is not None
        second_snapshot = async_worker._snapshot_task(second_claim)

        async def accept_callback(_context, _path, payload, **_kwargs):
            sent_payloads.append(payload)

        monkeypatch.setattr(
            async_worker,
            "_post_callback",
            accept_callback,
        )
        asyncio.run(
            async_worker._execute_snapshot(
                second_snapshot,
                object(),
            )
        )
        mark_async_task_done(session, task.id)

        assert sent_payloads == [callback_payload, callback_payload]
        assert session.get(type(task), task.id).status == "done"


def test_job_result_callback_requires_correlated_contract_ack(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "request_id": "req_0000000087654321",
                "status": "processed",
            }

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        async_worker.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )

    with pytest.raises(
        ValueError,
        match="ack_request_id_mismatch",
    ):
        asyncio.run(
            async_worker._post_callback(
                AgentCallbackContext(
                    app_base_url="http://backend.test",
                ),
                "/api/agent/async/missed-dose-results",
                {"request_id": "req_0000000012345678"},
                bearer=True,
            )
        )


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
            payload={"dose_event_id": 1, "patient_id": "patient-observability"},
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
        assert payload["payload_keys"] == ["dose_event_id", "patient_id"]
        assert payload["payload_parse_error"] is False
        assert payload["callback_context"] == {"job_id": 77}
        assert "patient-observability" not in json.dumps(payload, ensure_ascii=False)


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


def test_multiturn_side_effect_request_returns_internal_continuation_control():
    agent = MultiturnChatAgent(DeterministicTestProvider(), ToolRuntime(None))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="속이 메스꺼운데 약 때문일까?",
        current_time=datetime(2026, 4, 20, 9, 30),
        context={},
    )

    response = asyncio.run(
        agent.run("trace-chat-continuation", request.model_dump(mode="json"))
    )

    assert response.decision_type == "continuation_required"
    assert response.human_summary == ""
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert response.structured_payload["specialist_tool_calls"][0]["name"] == "get_medication_side_effect_assessment"
    assert response.structured_payload["continuation_required"] is True
    assert response.structured_payload["continuation_type"] == "side_effect_assessment"
    assert response.structured_payload["tool_calls"][0]["name"] == "get_medication_side_effect_assessment"


def test_multiturn_policy_request_returns_internal_continuation_control():
    agent = MultiturnChatAgent(DeterministicTestProvider(), ToolRuntime(None))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="아침 알림을 2번 10분 간격으로 바꿔줘",
        current_time=datetime(2026, 4, 20, 9, 30),
        context={},
    )

    response = asyncio.run(
        agent.run("trace-policy-continuation", request.model_dump(mode="json"))
    )

    assert response.decision_type == "continuation_required"
    assert response.human_summary == ""
    assert response.structured_payload["continuation_required"] is True
    assert response.structured_payload["continuation_type"] == "policy_change_request"
    assert response.structured_payload["policy_confirmation_required"] is True
    assert response.structured_payload["tool_calls"][0]["name"] == "propose_notification_policy"


def test_async_clinician_alert_callback_creates_internal_only_stub_once():
    with build_session() as session:
        ensure_clock(session)
        _seed_dose_event(session, event_id=77)
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
        _seed_dose_event(session, event_id=88)
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
