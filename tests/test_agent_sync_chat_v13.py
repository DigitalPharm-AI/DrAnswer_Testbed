from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, inspect, select
from sqlalchemy.orm import sessionmaker

from agent_app import main as agent_main
from agent_app import security as agent_security
from agent_app.errors import AgentExecutionError
from agent_app.integration.idempotency import (
    COMPLETED,
    FINAL_FAILED,
    PROCESSING,
    RETRYABLE_FAILED,
    IdempotencyConflictError,
    PatientThreadBusyError,
    RequestInProgressError,
    StaleSyncRequestAttemptError,
    StoredHttpResponse,
    SyncRequestClaim,
    SyncRequestGate,
)
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.migrations import (
    required_migration_versions,
    run_migrations,
)
from agent_app.persistence.models import (
    AgentAsyncTask,
    AgentFeedbackLink,
    AgentPatientLock,
    AgentPendingAction,
    AgentPendingSelection,
    AgentProCtcaeSurvey,
    AgentRunStep,
    AgentRunTrace,
    AgentSyncRequest,
    AgentToolExecution,
)
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.routes import chat as chat_routes
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.chat_contracts import (
    ChatStreamEvent,
    ChatSyncRequest,
    ChatSyncResponse,
    chat_sync_response,
)
from shared.schemas import AgentResponse
from shared.tool_names import CREATE_MEDICATION_SIDE_EFFECT_RECORD
from tests.helpers import build_agent_engine

NOW = datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
SYNC_HEADERS = {
    "Authorization": "Bearer pytest-agent-sync-token",
    "Accept": "application/x-ndjson",
}


class StubBackendQueryTools:
    def validate_chat_message(self, payload: ChatSyncRequest) -> dict:
        return {
            "backend_message_verified": True,
            "message_id": payload.message_id,
        }

    def patient_context_snapshot(
        self,
        *,
        patient_id: str,
        as_of: datetime,
    ) -> dict:
        return {
            "patient_id": patient_id,
            "as_of": as_of.isoformat(),
            "date": as_of.date().isoformat(),
            "read_contract_version": BACKEND_READ_CONTRACT_VERSION,
            "context_mode": "complete",
            "availability": {
                "profile": "not_found",
                "conditions_and_treatments": "not_found",
                "today_medication": "not_found",
                "today_meals": "not_found",
                "notification_policies": "not_found",
                "allergies": "not_supported",
                "clinical_observations": "not_supported",
            },
            "profile": None,
            "active_conditions": [],
            "active_treatments": [],
            "active_medication_schedules": [],
            "today_medication": {
                "schedules": [],
                "dose_events": [],
                "total": 0,
                "totals_by_status": {},
            },
            "today_meals": [],
            "active_notification_policies": [],
        }


@pytest.fixture(autouse=True)
def configured_backend_query_tools(monkeypatch):
    backend_queries = StubBackendQueryTools()
    monkeypatch.setattr(
        chat_routes,
        "_backend_query_tools_getter",
        lambda: backend_queries,
    )


def chat_request(
    *,
    request_id: str | None = None,
    patient_id: str = "patient_0000000000000001",
    message: str = "점심 기록을 도와줘.",
    requested_return_type: str = "text",
) -> ChatSyncRequest:
    suffix = uuid4().hex[:16]
    return ChatSyncRequest(
        request_id=request_id or f"req_{suffix}",
        message_id=f"user_msg_{suffix}",
        patient_id=patient_id,
        requested_return_type=requested_return_type,
        message=message,
        message_at=NOW,
    )


def assert_error_envelope(
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


def assert_single_error_terminal(
    response,
    request: ChatSyncRequest,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> ChatStreamEvent:
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/x-ndjson"
    )
    rows = [
        json.loads(line)
        for line in response.text.splitlines()
        if line
    ]
    assert len(rows) == 1
    terminal = ChatStreamEvent.model_validate(rows[0])
    assert terminal.request_id == request.request_id
    assert terminal.message_id == request.message_id
    assert terminal.sequence == 0
    assert terminal.status == "error"
    assert terminal.message_type is None
    assert terminal.delta is None
    assert terminal.message is None
    assert terminal.error is not None
    assert terminal.error.model_dump(mode="json") == {
        "code": code,
        "message": message,
        "retryable": retryable,
        "details": None,
    }
    return terminal


def ndjson_events(response) -> list[ChatStreamEvent]:
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/x-ndjson"
    )
    return [
        ChatStreamEvent.model_validate(json.loads(line))
        for line in response.text.splitlines()
        if line
    ]


def agent_response(
    *,
    trace_id: str = "internal-trace-001",
    summary: str = "처리했습니다.",
    structured_payload: dict | None = None,
) -> AgentResponse:
    return AgentResponse(
        trace_id=trace_id,
        agent_name="multiturn_chat_agent",
        prompt_version_id="test",
        decision_type="system_guidance",
        structured_payload=structured_payload or {},
        human_summary=summary,
    )


def gate() -> SyncRequestGate:
    return SyncRequestGate(
        SessionLocal,
        api_path="/agent/sync/chat",
        lock_lease_seconds=120,
        retention_seconds=86_400,
    )


def cleanup_request(request: ChatSyncRequest) -> None:
    with SessionLocal() as session:
        trace_ids = list(
            session.scalars(
                select(AgentRunTrace.trace_id).where(
                    AgentRunTrace.request_id == request.request_id
                )
            ).all()
        )
        if trace_ids:
            session.execute(
                delete(AgentRunStep).where(AgentRunStep.trace_id.in_(trace_ids))
            )
            session.execute(
                delete(AgentToolExecution).where(
                    AgentToolExecution.trace_id.in_(trace_ids)
                )
            )
        session.execute(
            delete(AgentFeedbackLink).where(
                AgentFeedbackLink.request_id == request.request_id
            )
        )
        session.execute(
            delete(AgentPendingAction).where(
                AgentPendingAction.source_chat_request_id == request.request_id
            )
        )
        session.execute(
            delete(AgentPendingSelection).where(
                (
                    AgentPendingSelection.source_chat_request_id
                    == request.request_id
                )
                | (
                    AgentPendingSelection.origin_message_id
                    == request.message_id
                )
            )
        )
        session.execute(
            delete(AgentProCtcaeSurvey).where(
                AgentProCtcaeSurvey.origin_message_id
                == request.message_id
            )
        )
        session.execute(
            delete(AgentRunTrace).where(
                AgentRunTrace.request_id == request.request_id
            )
        )
        session.execute(
            delete(AgentPatientLock).where(
                AgentPatientLock.patient_id == request.patient_id
            )
        )
        session.execute(
            delete(AgentSyncRequest).where(
                AgentSyncRequest.request_id == request.request_id
            )
        )
        session.commit()


def test_chat_contract_rejects_naive_datetime_and_invalid_input_box_json() -> None:
    base = {
        "request_id": "req_0000000000000001",
        "message_id": "user_msg_0000000000000001",
        "patient_id": "patient_0000000000000001",
        "requested_return_type": "text",
        "message": "안녕하세요.",
        "message_at": "2026-07-25T10:30:00",
    }
    with pytest.raises(ValidationError, match="timezone_offset_required"):
        ChatSyncRequest.model_validate(base)

    base["message_at"] = "2026-07-25T10:30:00+09:00"
    base["requested_return_type"] = "input_box"
    base["message"] = "{invalid-json}"
    with pytest.raises(
        ValidationError,
        match="input_box_message_must_be_valid_json_object_string",
    ):
        ChatSyncRequest.model_validate(base)

    base["message"] = '{"체중":"70","체중":"71"}'
    with pytest.raises(ValidationError, match="input_box_message_keys_must_be_unique"):
        ChatSyncRequest.model_validate(base)

    base["message"] = '{"체중":{"value":"70"}}'
    with pytest.raises(ValidationError, match="input_box_message_values_must_be_scalar"):
        ChatSyncRequest.model_validate(base)


@pytest.mark.parametrize(
    ("message_type", "message", "error"),
    [
        (
            "text",
            {
                "message_title": None,
                "text": "텍스트",
                "tables": None,
                "selections": ["금지"],
                "inputs": None,
            },
            "text_message_forbids_selections",
        ),
        (
            "selection_box",
            {
                "message_title": None,
                "text": "선택",
                "tables": None,
                "selections": ["예"],
                "inputs": [],
            },
            "selection_box_forbids_inputs",
        ),
        (
            "input_box",
            {
                "message_title": None,
                "text": "입력",
                "tables": None,
                "selections": [],
                "inputs": [
                    {
                        "type": "dropdown",
                        "label": "식사",
                        "value": None,
                        "options": {
                            "unit": None,
                            "lower": None,
                            "upper": None,
                            "selections": ["아침", "점심"],
                        },
                    }
                ],
            },
            "input_box_forbids_selections",
        ),
    ],
)
def test_chat_response_contract_forbids_message_type_incompatible_fields(
    message_type: str,
    message: dict,
    error: str,
) -> None:
    with pytest.raises(ValidationError, match=error):
        ChatSyncResponse.model_validate(
            {
                "request_id": "req_0000000000000002",
                "message_id": "user_msg_0000000000000002",
                "message_type": message_type,
                "message": message,
                "message_at": NOW,
            }
        )


def test_agent_sync_migrations_create_internal_state_tables(tmp_path: Path) -> None:
    migration_engine, cleanup = build_agent_engine(
        "agent_sync_migrations",
        create_models=False,
    )
    try:
        applied = run_migrations(migration_engine)
        table_names = set(inspect(migration_engine).get_table_names())
        assert "agent_sync_requests" in table_names
        assert "agent_patient_locks" in table_names
        assert "attempt_epoch" in {
            column["name"]
            for column in inspect(migration_engine).get_columns(
                "agent_sync_requests"
            )
        }
        assert "attempt_epoch" in {
            column["name"]
            for column in inspect(migration_engine).get_columns(
                "agent_patient_locks"
            )
        }
        assert "agent_conversation_locks" not in table_names
        assert "agent_run_traces" in table_names
        assert "agent_run_steps" in table_names
        assert "agent_tool_executions" in table_names
        assert "agent_pending_actions" in table_names
        assert "agent_feedback_links" in table_names
        assert "20260725_0003_agent_sync_requests" in applied
        assert "20260725_0020_agent_feedback_expiry_index" in applied
        assert required_migration_versions(migration_engine)[-1] in applied
    finally:
        cleanup()


def test_agent_state_retention_expires_pending_and_deletes_expired_records(
    tmp_path: Path,
) -> None:
    retention_engine, cleanup = build_agent_engine(
        "agent_retention"
    )
    retention_sessions = sessionmaker(
        bind=retention_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    now = datetime(2026, 7, 25, 12, 0)
    past = now - timedelta(seconds=1)
    future = now + timedelta(days=30)
    try:
        with retention_sessions() as session:
            session.add(
                AgentAsyncTask(
                    request_id="req_0000000020000000",
                    task_type="missed_dose",
                    status="done",
                    payload_json='{"private":"payload"}',
                    accepted_at=past,
                    completed_at=past,
                    expires_at=past,
                )
            )
            session.add(
                AgentRunTrace(
                    trace_id="expired-trace",
                    request_id="req_0000000020000001",
                    patient_id_hash="patient-hash",
                    workflow_name="multiturn_chat",
                    started_at=past,
                    created_at=past,
                    updated_at=past,
                    expires_at=past,
                )
            )
            session.flush()
            session.add(
                AgentRunStep(
                    trace_id="expired-trace",
                    sequence=1,
                    step_type="request_ingress",
                    created_at=past,
                    expires_at=future,
                )
            )
            session.add(
                AgentToolExecution(
                    trace_id="expired-trace",
                    request_id="req_0000000020000001",
                    patient_id_hash="patient-hash",
                    tool_call_id="tool-call-1",
                    tool_name="get_medication_dose_status",
                    created_at=past,
                    updated_at=past,
                    expires_at=future,
                )
            )
            session.add(
                AgentPendingAction(
                    public_id="pending-action-1",
                    source_chat_request_id="req_0000000020000002",
                    source_message_id="user_msg_0000000020000001",
                    trace_id="pending-trace",
                    patient_id_hash="patient-hash",
                    action_type="agent_tool",
                    action_name="update_medication_dose_event_status",
                    action_fingerprint="fingerprint-1",
                    status="PENDING",
                    created_at=past,
                    updated_at=past,
                    action_expires_at=past,
                    expires_at=future,
                )
            )
            session.add(
                AgentFeedbackLink(
                    api_path="/agent/async/chat_feedback",
                    request_id="req_0000000020000003",
                    message_id="assistant_msg_0000000020000001",
                    patient_id_hash="patient-hash",
                    feedback=None,
                    feedback_at=past,
                    created_at=past,
                    updated_at=past,
                    expires_at=past,
                )
            )
            session.add(
                AgentSyncRequest(
                    api_path="/agent/sync/chat",
                    request_id="req_0000000020000004",
                    request_hash="hash",
                    patient_id="patient_0000000020000004",
                    message_id="user_msg_0000000020000002",
                    status=COMPLETED,
                    created_at=past,
                    updated_at=past,
                    expires_at=past,
                )
            )
            session.add(
                AgentPatientLock(
                    patient_id="patient_0000000020000005",
                    request_id="req_0000000020000005",
                    lease_expires_at=past,
                    created_at=past,
                    updated_at=past,
                )
            )
            session.commit()

        result = purge_expired_agent_state(retention_sessions, now=now)

        assert result["expired_pending_actions"] == 1
        assert result["deleted_run_traces"] == 1
        assert result["deleted_run_steps"] == 1
        assert result["deleted_tool_executions"] == 1
        assert result["deleted_feedback_links"] == 1
        assert result["deleted_sync_requests"] == 1
        assert result["deleted_async_tasks"] == 1
        assert result["deleted_conversation_locks"] == 1
        with retention_sessions() as session:
            pending = session.scalar(
                select(AgentPendingAction).where(
                    AgentPendingAction.public_id == "pending-action-1"
                )
            )
            assert pending is not None
            assert pending.status == "EXPIRED"
            assert pending.error_code == "ACTION_EXPIRED"
    finally:
        cleanup()


def test_chat_response_hides_trace_and_echoes_backend_message_id() -> None:
    request = chat_request()
    response = chat_sync_response(request, agent_response())
    body = response.model_dump(mode="json")

    assert body["request_id"] == request.request_id
    assert body["message_id"] == request.message_id
    assert body["message_type"] == "text"
    assert body["message"]["text"] == "처리했습니다."
    assert "trace_id" not in body


def test_chat_response_maps_mutation_confirmation_to_selection_box() -> None:
    request = chat_request()
    response = chat_sync_response(
        request,
        agent_response(
            summary="변경 내용을 적용할까요?",
            structured_payload={
                "mutation_confirmation": {
                    "display": {
                        "title": "복약 기록 변경",
                        "question": "변경 내용을 적용할까요?",
                        "tables": [
                            {
                                "table_title": None,
                                "rows": [
                                    {
                                        "column": "변경 대상",
                                        "value": "아침 복약",
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
        ),
    )

    assert response.message_type == "selection_box"
    assert response.message.message_title == "복약 기록 변경"
    assert response.message.tables is not None
    assert response.message.tables[0].rows[0].column == "변경 대상"
    assert response.message.tables[0].rows[0].value == "아침 복약"
    assert response.message.selections == ["변경 적용", "취소"]
    assert "[확인]" not in (response.message.text or "")
    assert "[취소]" not in (response.message.text or "")
    assert "**기록**" not in (response.message.text or "")
    assert "**취소**" not in (response.message.text or "")


def test_chat_response_maps_pro_ctcae_tool_result_to_guided_selection_box() -> None:
    request = chat_request(message="어제 속이 좀 메스꺼웠어")
    response = chat_sync_response(
        request,
        agent_response(
            summary="질문이 준비되었습니다.",
            structured_payload={
                "ae_pro_ctcae": {
                    "input_symptom": "속이 메스꺼웠어",
                    "matched": True,
                    "matched_korean_symptom_name": "메스꺼움",
                    "questions": [
                        {
                            "korean_symptom_name": "메스꺼움",
                            "question": "지난 7일 동안 메스꺼움이 얼마나 자주 있었나요?",
                            "response_options": [
                                "전혀 없음",
                                "가끔",
                                "자주",
                                "거의 항상",
                            ],
                        }
                    ],
                }
            },
        ),
    )

    assert response.message_type == "selection_box"
    assert response.message.message_title == "메스꺼움 관련 자가 보고 설문"
    assert response.message.text == (
        "메스꺼움 증상이 약물과 관련이 있을 수 있습니다. "
        "아래의 질문에 답변해 주시면 증상을 더 정확하게 평가할 수 있습니다."
        "\n\n지난 7일 동안 메스꺼움이 얼마나 자주 있었나요?"
    )
    assert (
        "증상을 더 정확히 평가하기 위한 질문이 준비되고 있습니다."
        not in response.message.text
    )
    assert response.message.selections == ["전혀 없음", "가끔", "자주", "거의 항상"]


def test_chat_response_fails_closed_for_questionnaire_without_a_question() -> None:
    request = chat_request(message="증상이 있어")

    with pytest.raises(ValueError, match="ae_pro_ctcae_question_invalid"):
        chat_sync_response(
            request,
            agent_response(
                summary="질문이 준비되었습니다.",
                structured_payload={
                    "ae_pro_ctcae": {
                        "input_symptom": "메스꺼움",
                        "matched": True,
                        "questions": [],
                    }
                },
            ),
        )


def test_chat_response_preserves_structured_input_and_table_payload() -> None:
    request = chat_request()
    response = chat_sync_response(
        request,
        agent_response(
            summary="현재 값을 확인하고 수정해주세요.",
            structured_payload={
                "chat_response": {
                    "message_type": "input_box",
                    "message": {
                        "message_title": "복약 정보 확인",
                        "text": "현재 값을 확인하고 수정해주세요.",
                        "tables": [
                            {
                                "table_title": "현재 복약 정보",
                                "rows": [
                                    {"column": "복용 시간", "value": "08:00"},
                                    {"column": "상태", "value": "복용 전"},
                                ],
                            }
                        ],
                        "selections": None,
                        "inputs": [
                            {
                                "type": "number",
                                "label": "복용량",
                                "value": 1,
                                "options": {
                                    "unit": "정",
                                    "lower": 0.5,
                                    "upper": 3,
                                    "selections": None,
                                },
                            },
                            {
                                "type": "dropdown",
                                "label": "복용 시점",
                                "value": "아침",
                                "options": {
                                    "unit": None,
                                    "lower": None,
                                    "upper": None,
                                    "selections": ["아침", "점심", "저녁"],
                                },
                            },
                        ],
                    },
                }
            },
        ),
    )

    assert response.message_type == "input_box"
    assert response.message.tables is not None
    assert response.message.tables[0].rows[0].value == "08:00"
    assert response.message.inputs is not None
    assert [item.label for item in response.message.inputs] == ["복용량", "복용 시점"]


def test_trace_store_hashes_sensitive_tool_payloads_instead_of_storing_raw_values() -> None:
    request = chat_request(message="환자 민감 메시지")
    trace_id = f"trace-{uuid4().hex}"
    store = AgentTraceStore(SessionLocal)
    response = agent_response(
        trace_id=trace_id,
        structured_payload={
            "routing_mode": "direct_tool",
            "tool_calls": [
                {
                    "id": "tool-call-1",
                    "name": "get_medication_dose_status",
                    "arguments": {
                        "patient_id": "patient_0000000000000001",
                        "medication_name": "민감 약품명",
                    },
                }
            ],
            "tool_results": [
                {
                    "tool_name": "get_medication_dose_status",
                    "status": "success",
                    "response": {"medication_name": "민감 약품명"},
                    "error": "",
                }
            ],
        },
    )
    try:
        store.start_chat(
            request,
            trace_id=trace_id,
            api_path="/agent/sync/chat",
        )
        store.complete_chat(request, response)

        with SessionLocal() as session:
            trace = session.scalar(
                select(AgentRunTrace).where(AgentRunTrace.trace_id == trace_id)
            )
            tool = session.scalar(
                select(AgentToolExecution).where(
                    AgentToolExecution.trace_id == trace_id
                )
            )
            assert trace is not None
            assert tool is not None
            assert trace.input_hash
            assert trace.output_hash
            assert tool.argument_hash
            assert tool.response_hash
            persisted = " ".join(
                [
                    trace.metadata_json,
                    tool.metadata_json,
                    tool.argument_hash,
                    tool.response_hash,
                ]
            )
            assert "환자 민감 메시지" not in persisted
            assert "민감 약품명" not in persisted
            assert "patient_0000000000000001" not in persisted
    finally:
        cleanup_request(request)


def test_sync_gate_replays_completed_response_and_maps_internal_trace() -> None:
    request = chat_request()
    request_gate = gate()
    try:
        claim = request_gate.begin(request)
        assert isinstance(claim, SyncRequestClaim)
        body = {
            "request_id": request.request_id,
            "message_id": request.message_id,
            "message_type": "text",
            "message": {
                "message_title": None,
                "text": "완료",
                "tables": None,
                "selections": None,
                "inputs": None,
            },
            "message_at": "2026-07-25T10:30:01+00:00",
        }
        request_gate.complete(
            request,
            claim=claim,
            status_code=200,
            body=body,
            trace_id="internal-trace-42",
        )

        replay = request_gate.begin(request)
        assert isinstance(replay, StoredHttpResponse)
        assert replay.status_code == 200
        assert replay.body == body

        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == request.request_id
                )
            )
            assert row is not None
            assert row.status == COMPLETED
            assert row.trace_id == "internal-trace-42"
            assert "trace_id" not in replay.body
    finally:
        cleanup_request(request)


def test_sync_gate_enforces_request_and_patient_thread_locks() -> None:
    request = chat_request()
    same_patient = chat_request(patient_id=request.patient_id)
    request_gate = gate()
    try:
        assert isinstance(
            request_gate.begin(request),
            SyncRequestClaim,
        )
        with pytest.raises(RequestInProgressError):
            request_gate.begin(request)
        with pytest.raises(PatientThreadBusyError):
            request_gate.begin(same_patient)

        changed_body = request.model_copy(update={"message": "다른 본문"})
        with pytest.raises(IdempotencyConflictError):
            request_gate.begin(changed_body)

        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == request.request_id
                )
            )
            assert row is not None
            assert row.status == PROCESSING
    finally:
        cleanup_request(request)
        cleanup_request(same_patient)


def test_sync_gate_reprocesses_retryable_and_replays_final_failure() -> None:
    retryable_request = chat_request()
    final_request = chat_request(patient_id="patient_0000000030000001")
    request_gate = gate()
    error_body = {
        "request_id": retryable_request.request_id,
        "error": {
            "code": "BACKEND_DB_TIMEOUT",
            "message": "timeout",
            "retryable": True,
            "details": None,
        }
    }
    try:
        retryable_claim = request_gate.begin(retryable_request)
        assert isinstance(retryable_claim, SyncRequestClaim)
        request_gate.fail(
            retryable_request,
            claim=retryable_claim,
            status_code=504,
            body=error_body,
            error_code="BACKEND_DB_TIMEOUT",
            retryable=True,
        )
        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == retryable_request.request_id
                )
            )
            assert row is not None
            assert row.status == RETRYABLE_FAILED
        retry_claim = request_gate.begin(retryable_request)
        assert isinstance(retry_claim, SyncRequestClaim)
        assert (
            retry_claim.attempt_epoch
            == retryable_claim.attempt_epoch + 1
        )
        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id
                    == retryable_request.request_id
                )
            )
            assert row is not None
            assert row.status == PROCESSING
            assert row.attempt_epoch == retry_claim.attempt_epoch
            assert row.trace_id == ""
            assert row.response_status is None
            assert row.response_json == ""
            assert row.last_error_code == ""
            assert row.completed_at is None
            assert row.lease_expires_at is not None
            patient_lock = session.scalar(
                select(AgentPatientLock).where(
                    AgentPatientLock.patient_id
                    == retryable_request.patient_id
                )
            )
            assert patient_lock is not None
            assert patient_lock.request_id == retryable_request.request_id
            assert (
                patient_lock.attempt_epoch
                == retry_claim.attempt_epoch
            )

        final_claim = request_gate.begin(final_request)
        assert isinstance(final_claim, SyncRequestClaim)
        final_body = {
            "request_id": final_request.request_id,
            "error": {
                "code": "AI_PROCESSING_ERROR",
                "message": "failed",
                "retryable": False,
                "details": None,
            }
        }
        request_gate.fail(
            final_request,
            claim=final_claim,
            status_code=500,
            body=final_body,
            error_code="AI_PROCESSING_ERROR",
            retryable=False,
        )
        replay = request_gate.begin(final_request)
        assert isinstance(replay, StoredHttpResponse)
        assert replay.status_code == 500
        assert replay.body == final_body
        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == final_request.request_id
                )
            )
            assert row is not None
            assert row.status == FINAL_FAILED
    finally:
        cleanup_request(retryable_request)
        cleanup_request(final_request)


def test_sync_gate_fences_stale_attempt_after_lease_takeover() -> None:
    request = chat_request()
    request_gate = gate()
    body = {
        "request_id": request.request_id,
        "message_id": request.message_id,
        "status": "completed",
    }
    try:
        first_claim = request_gate.begin(request)
        assert isinstance(first_claim, SyncRequestClaim)
        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        with SessionLocal() as session:
            request_row = session.scalar(
                select(AgentSyncRequest)
                .where(
                    AgentSyncRequest.request_id == request.request_id
                )
                .with_for_update()
            )
            patient_lock = session.scalar(
                select(AgentPatientLock)
                .where(
                    AgentPatientLock.patient_id == request.patient_id
                )
                .with_for_update()
            )
            assert request_row is not None
            assert patient_lock is not None
            request_row.lease_expires_at = past
            patient_lock.lease_expires_at = past
            session.commit()

        second_claim = request_gate.begin(request)
        assert isinstance(second_claim, SyncRequestClaim)
        assert second_claim.attempt_epoch == first_claim.attempt_epoch + 1

        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:bind_trace",
        ):
            request_gate.bind_trace(
                request,
                "stale-trace",
                claim=first_claim,
            )
        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:finish",
        ):
            request_gate.complete(
                request,
                claim=first_claim,
                status_code=200,
                body=body,
                trace_id="stale-trace",
            )
        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:finish",
        ):
            request_gate.fail(
                request,
                claim=first_claim,
                status_code=503,
                body=body,
                error_code="STALE_FAILURE",
                retryable=True,
            )

        with SessionLocal() as session:
            request_row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == request.request_id
                )
            )
            patient_lock = session.scalar(
                select(AgentPatientLock).where(
                    AgentPatientLock.patient_id == request.patient_id
                )
            )
            assert request_row is not None
            assert request_row.status == PROCESSING
            assert request_row.attempt_epoch == second_claim.attempt_epoch
            assert request_row.trace_id == ""
            assert patient_lock is not None
            assert patient_lock.attempt_epoch == second_claim.attempt_epoch

        request_gate.complete(
            request,
            claim=second_claim,
            status_code=200,
            body=body,
            trace_id="current-trace",
        )
        with SessionLocal() as session:
            assert (
                session.scalar(
                    select(AgentPatientLock).where(
                        AgentPatientLock.patient_id
                        == request.patient_id
                    )
                )
                is None
            )
    finally:
        cleanup_request(request)


def test_sync_gate_fences_attempt_after_patient_lock_moves_to_other_request() -> None:
    first_request = chat_request()
    second_request = chat_request(patient_id=first_request.patient_id)
    request_gate = gate()
    stale_body = {
        "request_id": first_request.request_id,
        "message_id": first_request.message_id,
        "status": "stale",
    }
    try:
        first_claim = request_gate.begin(first_request)
        assert isinstance(first_claim, SyncRequestClaim)
        past = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=1
        )
        with SessionLocal() as session:
            patient_lock = session.scalar(
                select(AgentPatientLock)
                .where(
                    AgentPatientLock.patient_id
                    == first_request.patient_id
                )
                .with_for_update()
            )
            assert patient_lock is not None
            patient_lock.lease_expires_at = past
            session.commit()

        second_claim = request_gate.begin(second_request)
        assert isinstance(second_claim, SyncRequestClaim)

        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:bind_trace:patient_lock",
        ):
            request_gate.bind_trace(
                first_request,
                "stale-trace",
                claim=first_claim,
            )
        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:finish:patient_lock",
        ):
            request_gate.complete(
                first_request,
                claim=first_claim,
                status_code=200,
                body=stale_body,
                trace_id="stale-trace",
            )
        with pytest.raises(
            StaleSyncRequestAttemptError,
            match="stale_sync_request_attempt:finish:patient_lock",
        ):
            request_gate.fail(
                first_request,
                claim=first_claim,
                status_code=503,
                body=stale_body,
                error_code="STALE_FAILURE",
                retryable=True,
            )

        with SessionLocal() as session:
            first_row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id
                    == first_request.request_id
                )
            )
            second_row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id
                    == second_request.request_id
                )
            )
            patient_lock = session.scalar(
                select(AgentPatientLock).where(
                    AgentPatientLock.patient_id
                    == first_request.patient_id
                )
            )
            assert first_row is not None
            assert first_row.status == PROCESSING
            assert first_row.trace_id == ""
            assert second_row is not None
            assert second_row.status == PROCESSING
            assert patient_lock is not None
            assert patient_lock.request_id == second_request.request_id
            assert patient_lock.attempt_epoch == second_claim.attempt_epoch

        request_gate.complete(
            second_request,
            claim=second_claim,
            status_code=200,
            body={
                "request_id": second_request.request_id,
                "message_id": second_request.message_id,
                "status": "completed",
            },
            trace_id="current-trace",
        )
    finally:
        cleanup_request(first_request)
        cleanup_request(second_request)


def test_sync_chat_resolves_side_effect_continuation_before_returning(
    monkeypatch,
) -> None:
    request = chat_request(message="어제 속이 좀 메스꺼웠어")

    class ContinuationOrchestrator:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, str | None]] = []

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls.append((payload, trace_id))
            assert request_kind == "multiturn_chat"
            if len(self.calls) == 1:
                return agent_response(
                    trace_id=trace_id or "",
                    summary="증상 내용을 확인해서 문항을 준비할게요.",
                    structured_payload={
                        "tool_calls": [
                            {
                                "id": "side-effect-lookup-1",
                                "name": "get_medication_side_effect_assessment",
                                "arguments": {
                                    "symptom_text": "속이 메스꺼웠어",
                                },
                            }
                        ],
                        "continuation_required": True,
                        "continuation_type": "side_effect_assessment",
                    },
                )
            return agent_response(
                trace_id=trace_id or "",
                summary="메스꺼움 관련 질문이 준비되었습니다.",
                structured_payload={
                    "ae_pro_ctcae": {
                        "input_symptom": "속이 메스꺼웠어",
                        "matched": True,
                        "match_type": "exact",
                        "matched_symptom_term": "Nausea",
                        "matched_korean_symptom_name": "메스꺼움",
                        "similarity": 1.0,
                        "threshold": 0.7,
                        "scoring_method": "local_similarity",
                        "embedding_provider": "",
                        "sheet_name": "Nausea",
                        "questions": [
                            {
                                "symptom_term": "Nausea",
                                "korean_symptom_name": "메스꺼움",
                                "item_code": "PROCTCAE_NAUSEA_FREQ",
                                "question": "지난 7일 동안 메스꺼움이 얼마나 자주 있었나요?",
                                "response_type": "single_choice",
                                "response_options": [
                                    "전혀 없음",
                                    "가끔",
                                    "자주",
                                    "거의 항상",
                                ],
                                "pdf_page": 1,
                                "sheet_name": "Nausea",
                            }
                        ],
                        "candidates": [],
                    }
                },
            )

    orchestrator = ContinuationOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    try:
        with TestClient(agent_main.app) as client:
            response = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        events = ndjson_events(response)
        assert events[-1].status == "completed"
        body = events[-1].model_dump(mode="json")
        assert body["message_type"] == "selection_box"
        assert "메스꺼움 증상이 약물과 관련이 있을 수 있습니다." in body["message"]["text"]
        assert "지난 7일 동안 메스꺼움이 얼마나 자주 있었나요?" in body["message"]["text"]
        assert (
            "증상을 더 정확히 평가하기 위한 질문이 준비되고 있습니다."
            not in body["message"]["text"]
        )
        assert body["message"]["selections"] == ["전혀 없음", "가끔", "자주", "거의 항상"]
        assert len(orchestrator.calls) == 2
        first_payload, first_trace_id = orchestrator.calls[0]
        continuation_payload, continuation_trace_id = orchestrator.calls[1]
        assert first_trace_id
        assert continuation_trace_id == first_trace_id
        assert first_payload["context"]["request_metadata"]["request_id"] == request.request_id
        assert continuation_payload["context"]["execute_tool_continuation"] is True
        assert continuation_payload["context"]["continuation_type"] == "side_effect_assessment"
        assert continuation_payload["context"]["continuation_tool_calls"][0]["name"] == (
            "get_medication_side_effect_assessment"
        )
    finally:
        cleanup_request(request)


def test_sync_chat_advances_two_pro_ctcae_questions_then_requests_approval(
    monkeypatch,
) -> None:
    patient_id = "patient_0000000000000200"
    initial = chat_request(
        patient_id=patient_id,
        message="어제 약을 먹고 속이 메스꺼웠어",
    )
    first_answer = chat_request(
        patient_id=patient_id,
        message="자주 있다",
        requested_return_type="selection_box",
    )
    second_answer = chat_request(
        patient_id=patient_id,
        message="심했다",
        requested_return_type="selection_box",
    )

    class SurveyBackendQueries(StubBackendQueryTools):
        def validate_chat_message(
            self,
            payload: ChatSyncRequest,
        ) -> dict:
            base = super().validate_chat_message(payload)
            if payload.message_id == first_answer.message_id:
                base["structured_response_context"] = {
                    "kind": "structured_chat_response",
                    "response_type": "selection_box",
                    "response_value": payload.message,
                    "source_message_id": (
                        "assistant_msg_0000000000000200"
                    ),
                    "originating_user_message_id": (
                        initial.message_id
                    ),
                    "source_message": {
                        "message_type": "selection_box",
                        "message": {
                            "message_title": (
                                "메스꺼움 관련 자가 보고 설문 (1/2)"
                            ),
                            "selections": [
                                "전혀 없다",
                                "자주 있다",
                            ],
                        },
                    },
                }
            elif payload.message_id == second_answer.message_id:
                base["structured_response_context"] = {
                    "kind": "structured_chat_response",
                    "response_type": "selection_box",
                    "response_value": payload.message,
                    "source_message_id": (
                        "assistant_msg_0000000000000201"
                    ),
                    "originating_user_message_id": (
                        first_answer.message_id
                    ),
                    "source_message": {
                        "message_type": "selection_box",
                        "message": {
                            "message_title": (
                                "메스꺼움 관련 자가 보고 설문 (2/2)"
                            ),
                            "selections": [
                                "전혀 없었다",
                                "심했다",
                            ],
                        },
                    },
                }
            return base

        def patient_context_snapshot(
            self,
            *,
            patient_id: str,
            as_of: datetime,
        ) -> dict:
            snapshot = super().patient_context_snapshot(
                patient_id=patient_id,
                as_of=as_of,
            )
            snapshot["availability"]["today_medication"] = (
                "available"
            )
            snapshot["active_medication_schedules"] = [
                {
                    "medication_name": "메트포르민 500mg",
                    "treatment_area": "당뇨약",
                },
                {
                    "medication_name": "수니티닙 50mg",
                    "treatment_area": "신장암 치료약",
                },
            ]
            return snapshot

    class SurveyMedicationAgent:
        def __init__(self) -> None:
            self.calls: list[tuple[dict, list[dict]]] = []

        async def continue_with_tool_calls(
            self,
            trace_id: str,
            payload: dict,
            *,
            tool_calls: list[dict],
        ) -> AgentResponse:
            self.calls.append((payload, tool_calls))
            return agent_response(
                trace_id=trace_id,
                summary="부작용 평가 기록을 저장할까요?",
                structured_payload={
                    "mutation_confirmation": {
                        "action_name": (
                            CREATE_MEDICATION_SIDE_EFFECT_RECORD
                        ),
                        "display": {
                            "title": "부작용 평가 기록",
                            "question": (
                                "부작용 평가 기록을 저장할까요?"
                            ),
                            "action_label": "기록",
                        },
                    }
                },
            )

    class SurveyMultiturnAgent:
        def __init__(self) -> None:
            self.medication_agent = SurveyMedicationAgent()

    class SurveyOrchestrator:
        def __init__(self) -> None:
            self.calls = 0
            self.multiturn_chat_agent = SurveyMultiturnAgent()

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            assert request_kind == "multiturn_chat"
            return agent_response(
                trace_id=trace_id or "",
                summary="메스꺼움 관련 질문이 준비되었습니다.",
                structured_payload={
                    "tool_calls": [
                        {
                            "id": "side-effect-lookup",
                            "name": (
                                "get_medication_side_effect_assessment"
                            ),
                            "arguments": {
                                "symptom_text": (
                                    "어제 약을 먹고 속이 메스꺼웠어"
                                ),
                                "symptom_onset_text": "어제 복용 후",
                            },
                        },
                        {
                            "id": "pro-ctcae-questionnaire",
                            "name": "get_pro_ctcae_questionnaire",
                            "arguments": {
                                "symptom_text": (
                                    "어제 약을 먹고 속이 메스꺼웠어"
                                )
                            },
                        },
                    ],
                    "side_effect_lookup": {
                        "tool_name": (
                            "get_medication_side_effect_assessment"
                        ),
                        "status": "success",
                        "response": {
                            "suspected": True,
                            "matched_items": [
                                "메트포르민 500mg",
                                "수니티닙 50mg",
                            ],
                            "matched_effects": [
                                "메트포르민 500mg: 메스꺼움",
                                "수니티닙 50mg: 메스꺼움",
                            ],
                        },
                    },
                    "ae_pro_ctcae": {
                        "input_symptom": (
                            "어제 약을 먹고 속이 메스꺼웠어"
                        ),
                        "matched": True,
                        "match_type": "exact",
                        "matched_symptom_term": "Nausea",
                        "matched_korean_symptom_name": "메스꺼움",
                        "similarity": 1.0,
                        "threshold": 0.7,
                        "scoring_method": "local_similarity",
                        "embedding_provider": "",
                        "sheet_name": "Nausea",
                        "questions": [
                            {
                                "symptom_term": "Nausea",
                                "korean_symptom_name": "메스꺼움",
                                "item_code": "PROCTCAE_NAUSEA_FREQ",
                                "question": (
                                    "메스꺼움을 얼마나 자주 "
                                    "느꼈습니까?"
                                ),
                                "response_type": "single_choice",
                                "response_options": [
                                    "전혀 없다",
                                    "자주 있다",
                                ],
                                "pdf_page": 1,
                                "sheet_name": "Nausea",
                            },
                            {
                                "symptom_term": "Nausea",
                                "korean_symptom_name": "메스꺼움",
                                "item_code": "PROCTCAE_NAUSEA_SEV",
                                "question": (
                                    "가장 심했을 때 어느 "
                                    "정도였습니까?"
                                ),
                                "response_type": "single_choice",
                                "response_options": [
                                    "전혀 없었다",
                                    "심했다",
                                ],
                                "pdf_page": 1,
                                "sheet_name": "Nausea",
                            },
                        ],
                        "candidates": [],
                    },
                },
            )

    backend_queries = SurveyBackendQueries()
    orchestrator = SurveyOrchestrator()
    monkeypatch.setattr(
        chat_routes,
        "_backend_query_tools_getter",
        lambda: backend_queries,
    )
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    try:
        with TestClient(agent_main.app) as client:
            first_response = client.post(
                "/agent/sync/chat",
                json=initial.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            second_response = client.post(
                "/agent/sync/chat",
                json=first_answer.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            approval_response = client.post(
                "/agent/sync/chat",
                json=second_answer.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        first_terminal = ndjson_events(first_response)[-1]
        second_terminal = ndjson_events(second_response)[-1]
        approval_terminal = ndjson_events(approval_response)[-1]
        assert first_terminal.message is not None
        assert first_terminal.message.message_title == (
            "메스꺼움 관련 자가 보고 설문 (1/2)"
        )
        assert second_terminal.message is not None
        assert second_terminal.message.message_title == (
            "메스꺼움 관련 자가 보고 설문 (2/2)"
        )
        assert approval_terminal.message is not None
        assert approval_terminal.message.message_title == (
            "부작용 평가 기록"
        )
        assert approval_terminal.message.selections == ["기록", "취소"]
        assert orchestrator.calls == 1
        medication_calls = (
            orchestrator.multiturn_chat_agent.medication_agent.calls
        )
        assert len(medication_calls) == 1
        continuation_payload, continuation_tool_calls = medication_calls[0]
        completed = continuation_payload["context"][
            "completed_pro_ctcae_survey"
        ]
        assert [
            response["response_text"]
            for response in completed["severity"]["responses"]
        ] == ["자주 있다", "심했다"]
        assert completed["medication_name"] is None
        assert continuation_tool_calls[0]["name"] == (
            "request_record_approval"
        )
        with SessionLocal() as session:
            survey = session.scalar(
                select(AgentProCtcaeSurvey).where(
                    AgentProCtcaeSurvey.origin_message_id
                    == initial.message_id
                )
            )
            assert survey is not None
            assert survey.status == "APPROVAL_PENDING"
    finally:
        cleanup_request(second_answer)
        cleanup_request(first_answer)
        cleanup_request(initial)


def test_food_selection_reuses_agent_db_candidate_without_search_llm(
    monkeypatch,
) -> None:
    patient_id = "patient_0000000000000710"
    initial = chat_request(
        patient_id=patient_id,
        message="오늘 아침에 토스트 먹었는데 기록해줘",
    )
    selection = chat_request(
        patient_id=patient_id,
        message="토스트(식빵)",
        requested_return_type="selection_box",
    )
    candidates = [
        {
            "food_ref_id": "D402-145000000-0001",
            "food_name": "토스트(식빵)",
            "category": "빵 및 과자류",
            "portion": "230.3g",
            "nutrients": {
                "calories": 84.0,
                "carbohydrates": 15.55,
                "protein": 2.5,
                "fat": 1.33,
                "sodium": 140.0,
            },
            "source": "식품의약품안전처",
            "manufacturer": "해당없음",
        }
    ]

    class FoodSelectionBackendQueries(
        StubBackendQueryTools
    ):
        def validate_chat_message(
            self,
            payload: ChatSyncRequest,
        ) -> dict:
            base = super().validate_chat_message(payload)
            if payload.message_id == selection.message_id:
                base["structured_response_context"] = {
                    "kind": "structured_chat_response",
                    "response_type": "selection_box",
                    "response_value": payload.message,
                    "source_message_id": (
                        "assistant_msg_0000000000000710"
                    ),
                    "originating_user_message_id": (
                        initial.message_id
                    ),
                    "source_message": {
                        "message_type": "selection_box",
                        "message": {
                            "message_title": "항목 선택",
                            "text": "토스트 종류를 선택해 주세요.",
                            "selections": ["토스트(식빵)"],
                        },
                    },
                }
            return base

    class FoodSelectionOrchestrator:
        def __init__(self) -> None:
            self.invoke_count = 0
            self.continuations: list[dict] = []

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.invoke_count += 1
            assert request_kind == "multiturn_chat"
            return agent_response(
                trace_id=trace_id or "",
                summary="토스트 종류를 선택해 주세요.",
                structured_payload={
                    "food_candidates": candidates,
                    "food_searches": [
                        {
                            "query": "토스트",
                            "meal_type": "breakfast",
                            "candidates": candidates,
                        }
                    ],
                },
            )

        async def continue_nutrition_food_selection(
            self,
            *,
            trace_id: str,
            payload: dict,
            record_arguments: dict,
            selection_id: str,
            origin_message_id: str,
        ) -> AgentResponse:
            self.continuations.append(
                {
                    "payload": payload,
                    "record_arguments": record_arguments,
                    "selection_id": selection_id,
                    "origin_message_id": origin_message_id,
                }
            )
            return agent_response(
                trace_id=trace_id,
                summary=(
                    "토스트(식빵) 식사 내용을 "
                    "데이터베이스에 새로 추가할까요?"
                ),
                structured_payload={
                    "routing_mode": (
                        "trusted_food_selection_continuation"
                    ),
                    "mutation_confirmation_required": True,
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": (
                            "create_nutrition_meal_record"
                        ),
                        "status": "pending",
                        "display": {
                            "title": "식사 기록",
                            "question": (
                                "토스트(식빵) 식사 내용을 "
                                "데이터베이스에 새로 추가할까요?"
                            ),
                            "action_label": "기록",
                        },
                    },
                    "selection_state_resolution": {
                        "reason_code": (
                            "TRUSTED_FOOD_SELECTION_STATE_REUSED"
                        ),
                        "candidate_reused": True,
                        "search_repeated": False,
                    },
                },
            )

    backend_queries = FoodSelectionBackendQueries()
    orchestrator = FoodSelectionOrchestrator()
    monkeypatch.setattr(
        chat_routes,
        "_backend_query_tools_getter",
        lambda: backend_queries,
    )
    monkeypatch.setattr(
        agent_main,
        "orchestrator",
        orchestrator,
    )
    try:
        with TestClient(agent_main.app) as client:
            first_response = client.post(
                "/agent/sync/chat",
                json=initial.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            selection_response = client.post(
                "/agent/sync/chat",
                json=selection.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        first_terminal = ndjson_events(first_response)[-1]
        approval_terminal = ndjson_events(
            selection_response
        )[-1]
        assert first_terminal.message_type == "selection_box"
        assert first_terminal.message is not None
        assert first_terminal.message.selections == [
            "토스트(식빵)"
        ]
        assert approval_terminal.message_type == (
            "selection_box"
        )
        assert approval_terminal.message is not None
        assert approval_terminal.message.selections == [
            "기록",
            "취소",
        ]

        assert orchestrator.invoke_count == 1
        assert len(orchestrator.continuations) == 1
        continuation = orchestrator.continuations[0]
        assert continuation["origin_message_id"] == (
            initial.message_id
        )
        assert continuation["record_arguments"] == {
            "meal_type": "breakfast",
            "meal_date": "2026-07-25",
            "foods": [
                {
                    "food_ref_id": (
                        "D402-145000000-0001"
                    ),
                    "food_name": "토스트(식빵)",
                    "portion": "230.3g",
                    "nutrients": {
                        "calories": 84.0,
                        "protein": 2.5,
                        "sodium": 140.0,
                        "fat": 1.33,
                        "carbohydrates": 15.55,
                    },
                }
            ],
        }
        with SessionLocal() as session:
            state = session.scalar(
                select(AgentPendingSelection).where(
                    AgentPendingSelection.origin_message_id
                    == initial.message_id
                )
            )
            assert state is not None
            assert state.status == "CONSUMED"
    finally:
        cleanup_request(selection)
        cleanup_request(initial)


def test_food_selection_batch_uses_sequential_cards_and_one_approval(
    monkeypatch,
) -> None:
    patient_id = "patient_0000000000000711"
    initial = chat_request(
        patient_id=patient_id,
        message=(
            "오늘 아침에 토스트와 삶은 계란을 "
            "먹었는데 기록해줘"
        ),
    )
    first_selection = chat_request(
        patient_id=patient_id,
        message="토스트(식빵)",
        requested_return_type="selection_box",
    )
    second_selection = chat_request(
        patient_id=patient_id,
        message="달걀_삶은 달걀",
        requested_return_type="selection_box",
    )
    toast = {
        "food_ref_id": "food-toast",
        "food_name": "토스트(식빵)",
        "portion": "1장",
        "nutrients": {"calories": 120.0},
    }
    egg = {
        "food_ref_id": "food-egg",
        "food_name": "달걀_삶은 달걀",
        "portion": "1개",
        "nutrients": {
            "calories": 75.0,
            "protein": 6.0,
        },
    }

    class BatchSelectionBackendQueries(
        StubBackendQueryTools
    ):
        def validate_chat_message(
            self,
            payload: ChatSyncRequest,
        ) -> dict:
            base = super().validate_chat_message(payload)
            if payload.message_id == first_selection.message_id:
                base["structured_response_context"] = {
                    "kind": "structured_chat_response",
                    "response_type": "selection_box",
                    "response_value": payload.message,
                    "source_message_id": (
                        "assistant_msg_0000000000000711"
                    ),
                    "originating_user_message_id": (
                        initial.message_id
                    ),
                    "source_message": {
                        "message_type": "selection_box",
                        "message": {
                            "message_title": (
                                "항목 선택 (1/2)"
                            ),
                            "text": (
                                "토스트 후보를 선택해 주세요."
                            ),
                            "selections": [
                                "토스트(식빵)"
                            ],
                        },
                    },
                }
            elif (
                payload.message_id
                == second_selection.message_id
            ):
                base["structured_response_context"] = {
                    "kind": "structured_chat_response",
                    "response_type": "selection_box",
                    "response_value": payload.message,
                    "source_message_id": (
                        "assistant_msg_0000000000000712"
                    ),
                    "originating_user_message_id": (
                        first_selection.message_id
                    ),
                    "source_message": {
                        "message_type": "selection_box",
                        "message": {
                            "message_title": (
                                "항목 선택 (2/2)"
                            ),
                            "text": (
                                "삶은 계란 후보를 "
                                "선택해 주세요."
                            ),
                            "selections": [
                                "달걀_삶은 달걀"
                            ],
                        },
                    },
                }
            return base

    class BatchSelectionOrchestrator:
        def __init__(self) -> None:
            self.invoke_count = 0
            self.continuations: list[dict] = []

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.invoke_count += 1
            return agent_response(
                trace_id=trace_id or "",
                summary="음식 후보를 차례대로 선택해 주세요.",
                structured_payload={
                    "food_candidates": [toast],
                    "food_selection_progress": {
                        "current_group": 1,
                        "total_groups": 2,
                        "query": "토스트",
                    },
                    "food_searches": [
                        {
                            "query": "토스트",
                            "meal_type": "breakfast",
                            "candidates": [toast],
                        },
                        {
                            "query": "삶은 계란",
                            "meal_type": "breakfast",
                            "candidates": [egg],
                        },
                    ],
                },
            )

        async def continue_nutrition_food_selection(
            self,
            *,
            trace_id: str,
            payload: dict,
            record_arguments: dict,
            selection_id: str,
            origin_message_id: str,
        ) -> AgentResponse:
            self.continuations.append(
                {
                    "record_arguments": record_arguments,
                    "selection_id": selection_id,
                    "origin_message_id": origin_message_id,
                }
            )
            return agent_response(
                trace_id=trace_id,
                summary=(
                    "아침 토스트(식빵), 달걀_삶은 "
                    "달걀 식사 내용을 기록할까요?"
                ),
                structured_payload={
                    "mutation_confirmation_required": True,
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": (
                            "create_nutrition_meal_record"
                        ),
                        "status": "pending",
                        "display": {
                            "title": "식사 기록",
                            "question": (
                                "아침 토스트(식빵), "
                                "달걀_삶은 달걀 식사를 "
                                "기록할까요?"
                            ),
                            "action_label": "기록",
                        },
                    },
                },
            )

    orchestrator = BatchSelectionOrchestrator()
    monkeypatch.setattr(
        chat_routes,
        "_backend_query_tools_getter",
        lambda: BatchSelectionBackendQueries(),
    )
    monkeypatch.setattr(
        agent_main,
        "orchestrator",
        orchestrator,
    )
    try:
        with TestClient(agent_main.app) as client:
            initial_response = client.post(
                "/agent/sync/chat",
                json=initial.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            first_response = client.post(
                "/agent/sync/chat",
                json=first_selection.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            final_response = client.post(
                "/agent/sync/chat",
                json=second_selection.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        first_card = ndjson_events(initial_response)[-1]
        second_card = ndjson_events(first_response)[-1]
        approval_card = ndjson_events(final_response)[-1]
        assert first_card.message is not None
        assert first_card.message.message_title == (
            "항목 선택 (1/2)"
        )
        assert first_card.message.selections == [
            "토스트(식빵)"
        ]
        assert second_card.message is not None
        assert second_card.message.message_title == (
            "항목 선택 (2/2)"
        )
        assert second_card.message.selections == [
            "달걀_삶은 달걀"
        ]
        assert approval_card.message is not None
        assert approval_card.message.selections == [
            "기록",
            "취소",
        ]
        assert orchestrator.invoke_count == 1
        assert len(orchestrator.continuations) == 1
        assert orchestrator.continuations[0][
            "record_arguments"
        ] == {
            "meal_type": "breakfast",
            "meal_date": "2026-07-25",
            "foods": [
                {
                    "food_ref_id": "food-toast",
                    "food_name": "토스트(식빵)",
                    "portion": "1장",
                    "nutrients": {
                        "calories": 120.0,
                    },
                },
                {
                    "food_ref_id": "food-egg",
                    "food_name": "달걀_삶은 달걀",
                    "portion": "1개",
                    "nutrients": {
                        "calories": 75.0,
                        "protein": 6.0,
                    },
                },
            ],
        }
    finally:
        cleanup_request(second_selection)
        cleanup_request(first_selection)
        cleanup_request(initial)


def test_sync_chat_fails_closed_when_continuation_repeats(monkeypatch) -> None:
    request = chat_request(message="어제 속이 좀 메스꺼웠어")

    class RepeatingContinuationOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            assert request_kind == "multiturn_chat"
            return agent_response(
                trace_id=trace_id or "",
                summary="이 문장은 외부 성공 응답으로 반환되면 안 됩니다.",
                structured_payload={
                    "tool_calls": [
                        {
                            "id": f"side-effect-lookup-{self.calls}",
                            "name": "get_medication_side_effect_assessment",
                            "arguments": {"symptom_text": "메스꺼움"},
                        }
                    ],
                    "continuation_required": True,
                    "continuation_type": "side_effect_assessment",
                },
            )

    orchestrator = RepeatingContinuationOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    try:
        with TestClient(agent_main.app) as client:
            response = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        assert_single_error_terminal(
            response,
            request,
            code="AI_PROCESSING_ERROR",
            message="An internal AI Server processing error occurred.",
            retryable=True,
        )
        assert "외부 성공 응답" not in response.text
        assert orchestrator.calls == 2
    finally:
        cleanup_request(request)


def test_sync_chat_timeout_covers_continuation_execution(monkeypatch) -> None:
    request = chat_request(message="어제 속이 좀 메스꺼웠어")
    settings = chat_routes.get_settings().model_copy(
        update={"agent_sync_chat_timeout_seconds": 0.1}
    )

    class SlowContinuationOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            assert request_kind == "multiturn_chat"
            if self.calls > 1:
                await asyncio.sleep(0.2)
            return agent_response(
                trace_id=trace_id or "",
                summary="완료 전 임시 응답",
                structured_payload={
                    "tool_calls": [
                        {
                            "id": "side-effect-lookup-1",
                            "name": "get_medication_side_effect_assessment",
                            "arguments": {"symptom_text": "메스꺼움"},
                        }
                    ],
                    "continuation_required": True,
                    "continuation_type": "side_effect_assessment",
                },
            )

    orchestrator = SlowContinuationOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    monkeypatch.setattr(chat_routes, "get_settings", lambda: settings)
    try:
        with TestClient(agent_main.app) as client:
            response = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        assert_single_error_terminal(
            response,
            request,
            code="AI_PROCESSING_TIMEOUT",
            message="The AI request exceeded the processing time limit.",
            retryable=True,
        )
        assert "완료 전 임시 응답" not in response.text
        assert orchestrator.calls == 2
    finally:
        cleanup_request(request)


def test_sync_chat_retryable_failure_is_reprocessed_with_same_request_id(
    monkeypatch,
) -> None:
    request = chat_request(message="암로디핀을 먹고 속이 메스꺼웠어")

    class TransientProviderFailureOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            assert request_kind == "multiturn_chat"
            if self.calls == 1:
                raise AgentExecutionError(
                    "temporary provider failure",
                    error_type="provider_request_failed",
                    trace_id=trace_id or "",
                    agent_name="multiturn_chat_agent",
                    decision_type="system_guidance",
                )
            return agent_response(
                trace_id=trace_id or "",
                summary="현재 복약 정보를 기준으로 증상을 확인했습니다.",
            )

    orchestrator = TransientProviderFailureOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    try:
        with TestClient(agent_main.app) as client:
            first = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            second = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        assert_single_error_terminal(
            first,
            request,
            code="LLM_PROVIDER_REQUEST_FAILED",
            message="The LLM provider request failed.",
            retryable=True,
        )
        events = ndjson_events(second)
        terminal = events[-1]
        assert terminal.status == "completed"
        assert terminal.error is None
        assert terminal.message is not None
        assert (
            terminal.message.text
            == "현재 복약 정보를 기준으로 증상을 확인했습니다."
        )
        assert second.content != first.content
        assert orchestrator.calls == 2
        with SessionLocal() as session:
            row = session.scalar(
                select(AgentSyncRequest).where(
                    AgentSyncRequest.request_id == request.request_id
                )
            )
            assert row is not None
            assert row.status == COMPLETED
    finally:
        cleanup_request(request)


def test_sync_chat_http_replays_exact_success_without_second_agent_call(monkeypatch) -> None:
    request = chat_request()

    class StubOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            assert request_kind == "multiturn_chat"
            assert payload["context"]["request_metadata"]["request_id"] == request.request_id
            assert trace_id
            with SessionLocal() as session:
                row = session.scalar(
                    select(AgentSyncRequest).where(
                        AgentSyncRequest.request_id == request.request_id
                    )
                )
                assert row is not None
                assert row.status == PROCESSING
                assert row.trace_id == trace_id
            return agent_response(trace_id=trace_id, summary="HTTP 완료")

    stub = StubOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", stub)
    try:
        with TestClient(agent_main.app) as client:
            first = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            second = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        first_events = ndjson_events(first)
        second_events = ndjson_events(second)
        assert first.content == second.content
        assert [event.model_dump(mode="json") for event in first_events] == [
            event.model_dump(mode="json") for event in second_events
        ]
        assert first_events[-1].message_id == request.message_id
        assert "trace_id" not in first.text
        assert stub.calls == 1
        with SessionLocal() as session:
            trace = session.scalar(
                select(AgentRunTrace).where(
                    AgentRunTrace.request_id == request.request_id
                )
            )
            assert trace is not None
            assert trace.status == "COMPLETED"
            assert trace.trace_id
            assert trace.message_id == request.message_id
            assert trace.input_hash
            assert request.message not in trace.metadata_json
    finally:
        cleanup_request(request)


def test_sync_chat_http_returns_contract_errors_for_invalid_and_conflicting_body(
    monkeypatch,
) -> None:
    request = chat_request()

    class StubOrchestrator:
        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            return agent_response(trace_id=trace_id or "fallback-trace")

    monkeypatch.setattr(agent_main, "orchestrator", StubOrchestrator())
    try:
        with TestClient(agent_main.app) as client:
            invalid_body = request.model_dump(mode="json")
            invalid_body["message_at"] = "2026-07-25T10:30:00"
            invalid = client.post(
                "/agent/sync/chat",
                json=invalid_body,
                headers=SYNC_HEADERS,
            )

            first = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            conflicting_body = request.model_dump(mode="json")
            conflicting_body["message"] = "변경된 본문"
            conflict = client.post(
                "/agent/sync/chat",
                json=conflicting_body,
                headers=SYNC_HEADERS,
            )

        assert invalid.status_code == 400
        assert_error_envelope(
            invalid,
            request_id=request.request_id,
            code="INVALID_REQUEST",
        )
        assert first.status_code == 200
        assert conflict.status_code == 409
        assert_error_envelope(
            conflict,
            request_id=request.request_id,
            code="IDEMPOTENCY_CONFLICT",
        )
    finally:
        cleanup_request(request)


def test_sync_chat_http_exposes_request_and_patient_thread_lock_errors(monkeypatch) -> None:
    request = chat_request()
    competing = chat_request(patient_id=request.patient_id)

    class UnexpectedOrchestrator:
        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            raise AssertionError("locked requests must not invoke the agent")

    monkeypatch.setattr(agent_main, "orchestrator", UnexpectedOrchestrator())
    request_gate = gate()
    try:
        assert isinstance(
            request_gate.begin(request),
            SyncRequestClaim,
        )
        with TestClient(agent_main.app) as client:
            in_progress = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )
            conversation_busy = client.post(
                "/agent/sync/chat",
                json=competing.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        assert in_progress.status_code == 409
        assert in_progress.headers["Retry-After"] == "1"
        assert_error_envelope(
            in_progress,
            request_id=request.request_id,
            code="REQUEST_IN_PROGRESS",
        )
        assert conversation_busy.status_code == 409
        assert conversation_busy.headers["Retry-After"] == "1"
        assert_error_envelope(
            conversation_busy,
            request_id=competing.request_id,
            code="CONVERSATION_BUSY",
        )
    finally:
        cleanup_request(request)
        cleanup_request(competing)


def test_sync_chat_http_accepts_shared_service_token(monkeypatch) -> None:
    request = chat_request()

    class TokenSettings:
        agent_sync_api_token = "sync-secret"
        internal_api_token = "internal-secret"

        @staticmethod
        def require_service_api_token() -> str:
            return "sync-secret"

    class StubOrchestrator:
        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            return agent_response(trace_id=trace_id or "fallback-trace")

    monkeypatch.setattr(agent_security, "get_settings", lambda: TokenSettings())
    monkeypatch.setattr(agent_main, "orchestrator", StubOrchestrator())
    try:
        with TestClient(agent_main.app) as client:
            missing = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
            )
            wrong = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer wrong-secret"},
            )
            unrelated_service_token = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer backend-secret"},
            )
            internal_fallback = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers={"Authorization": "Bearer internal-secret"},
            )
            accepted = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers={
                    "Authorization": "Bearer sync-secret",
                    "Accept": "application/x-ndjson",
                },
            )

        expected_error = {
            "request_id": request.request_id,
            "error": {
                "code": "UNAUTHORIZED",
                "message": "Authorization failed.",
                "retryable": False,
                "details": None,
            }
        }
        assert missing.status_code == 401
        assert missing.json() == expected_error
        assert wrong.status_code == 401
        assert wrong.json() == expected_error
        assert unrelated_service_token.status_code == 401
        assert unrelated_service_token.json() == expected_error
        assert internal_fallback.status_code == 401
        assert internal_fallback.json() == expected_error
        assert accepted.status_code == 200
    finally:
        cleanup_request(request)


def test_sync_chat_fails_closed_when_backend_query_tools_are_unavailable(monkeypatch) -> None:
    request = chat_request()

    class UnexpectedOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        async def invoke(
            self,
            request_kind: str,
            payload: dict,
            *,
            trace_id: str | None = None,
        ) -> AgentResponse:
            self.calls += 1
            raise AssertionError("unavailable BackendQueryTools must block orchestration")

    orchestrator = UnexpectedOrchestrator()
    monkeypatch.setattr(agent_main, "orchestrator", orchestrator)
    monkeypatch.setattr(chat_routes, "_backend_query_tools_getter", lambda: None)
    try:
        with TestClient(agent_main.app) as client:
            response = client.post(
                "/agent/sync/chat",
                json=request.model_dump(mode="json"),
                headers=SYNC_HEADERS,
            )

        assert response.status_code == 503
        assert response.json() == {
            "request_id": request.request_id,
            "error": {
                "code": "BACKEND_DB_UNAVAILABLE",
                "message": (
                    "The read-only Backend DB connection is unavailable or "
                    "incompatible."
                ),
                "retryable": True,
                "details": None,
            }
        }
        assert orchestrator.calls == 0
    finally:
        cleanup_request(request)
