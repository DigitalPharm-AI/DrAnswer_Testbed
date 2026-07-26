from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete, inspect, select

from agent_app import main as agent_main
from agent_app import security as agent_security
from agent_app.integration.chat_contracts import (
    ChatSyncRequest,
    ChatSyncResponse,
    chat_sync_response,
)
from agent_app.integration.idempotency import (
    COMPLETED,
    FINAL_FAILED,
    PROCESSING,
    RETRYABLE_FAILED,
    ConversationBusyError,
    IdempotencyConflictError,
    RequestInProgressError,
    SyncRequestGate,
)
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import (
    AgentConversationLock,
    AgentFeedbackLink,
    AgentPendingAction,
    AgentRunStep,
    AgentRunTrace,
    AgentSyncRequest,
    AgentToolExecution,
)
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.routes import chat as chat_routes
from shared.db import create_session_factory
from shared.schemas import AgentResponse

NOW = datetime(2026, 7, 25, 10, 30, tzinfo=UTC)
SYNC_HEADERS = {"Authorization": "Bearer pytest-agent-sync-token"}


class StubBackendQueryTools:
    def validate_chat_message(self, payload: ChatSyncRequest) -> dict:
        return {
            "backend_message_verified": True,
            "message_id": payload.message_id,
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
    conversation_id: str | None = None,
    message: str = "점심 기록을 도와줘.",
    requested_return_type: str | None = None,
) -> ChatSyncRequest:
    suffix = uuid4().hex
    return ChatSyncRequest(
        request_id=request_id or f"req-{suffix}",
        message_id=f"user-msg-{suffix}",
        conversation_id=conversation_id or f"conv-{suffix}",
        patient_id="patient-123",
        requested_return_type=requested_return_type,
        message=message,
        message_at=NOW,
    )


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
            delete(AgentRunTrace).where(
                AgentRunTrace.request_id == request.request_id
            )
        )
        session.execute(
            delete(AgentConversationLock).where(
                AgentConversationLock.conversation_id == request.conversation_id
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
        "request_id": "req-1",
        "message_id": "user-msg-1",
        "conversation_id": "conv-1",
        "patient_id": "patient-1",
        "requested_return_type": None,
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
                "request_id": "request-contract-validation",
                "message_id": "message-contract-validation",
                "message_type": message_type,
                "message": message,
                "message_at": NOW,
            }
        )


def test_agent_sync_migrations_create_internal_state_tables(tmp_path: Path) -> None:
    database_path = (tmp_path / "agent-sync-migrations.db").as_posix()
    migration_engine, _ = create_session_factory(f"sqlite:///{database_path}")
    try:
        applied = run_migrations(migration_engine)
        table_names = set(inspect(migration_engine).get_table_names())
        assert "agent_sync_requests" in table_names
        assert "agent_conversation_locks" in table_names
        assert "agent_run_traces" in table_names
        assert "agent_run_steps" in table_names
        assert "agent_tool_executions" in table_names
        assert "agent_pending_actions" in table_names
        assert "agent_feedback_links" in table_names
        assert "20260725_0003_agent_sync_requests" in applied
        assert "20260725_0020_agent_feedback_expiry_index" in applied
    finally:
        migration_engine.dispose()


def test_agent_state_retention_expires_pending_and_deletes_expired_records(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "agent-retention.db").as_posix()
    retention_engine, retention_sessions = create_session_factory(
        f"sqlite:///{database_path}"
    )
    now = datetime(2026, 7, 25, 12, 0)
    past = now - timedelta(seconds=1)
    future = now + timedelta(days=30)
    try:
        from agent_app.persistence.models import Base

        Base.metadata.create_all(bind=retention_engine)
        with retention_sessions() as session:
            session.add(
                AgentRunTrace(
                    trace_id="expired-trace",
                    request_id="expired-request",
                    conversation_id="expired-conversation",
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
                    request_id="expired-request",
                    conversation_id="expired-conversation",
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
                    source_chat_request_id="pending-request",
                    source_message_id="user-message-1",
                    trace_id="pending-trace",
                    conversation_id="pending-conversation",
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
                    request_id="feedback-request",
                    message_id="assistant-message-1",
                    conversation_id="feedback-conversation",
                    patient_id_hash="patient-hash",
                    feedback=True,
                    feedback_at=past,
                    created_at=past,
                    updated_at=past,
                    expires_at=past,
                )
            )
            session.add(
                AgentSyncRequest(
                    api_path="/agent/sync/chat",
                    request_id="expired-sync-request",
                    request_hash="hash",
                    conversation_id="sync-conversation",
                    patient_id="patient-1",
                    message_id="user-message-2",
                    status=COMPLETED,
                    created_at=past,
                    updated_at=past,
                    expires_at=past,
                )
            )
            session.add(
                AgentConversationLock(
                    conversation_id="expired-lock-conversation",
                    request_id="expired-lock-request",
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
        retention_engine.dispose()


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
                    }
                }
            },
        ),
    )

    assert response.message_type == "selection_box"
    assert response.message.message_title == "복약 기록 변경"
    assert response.message.selections == ["변경 적용", "취소"]


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
                        "patient_id": "patient-123",
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
            assert "patient-123" not in persisted
    finally:
        cleanup_request(request)


def test_sync_gate_replays_completed_response_and_maps_internal_trace() -> None:
    request = chat_request()
    request_gate = gate()
    try:
        assert request_gate.begin(request) is None
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
            status_code=200,
            body=body,
            trace_id="internal-trace-42",
        )

        replay = request_gate.begin(request)
        assert replay is not None
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


def test_sync_gate_enforces_request_and_conversation_locks() -> None:
    request = chat_request()
    same_conversation = chat_request(conversation_id=request.conversation_id)
    request_gate = gate()
    try:
        assert request_gate.begin(request) is None
        with pytest.raises(RequestInProgressError):
            request_gate.begin(request)
        with pytest.raises(ConversationBusyError):
            request_gate.begin(same_conversation)

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
        cleanup_request(same_conversation)


def test_sync_gate_allows_retryable_failure_but_replays_final_failure() -> None:
    retryable_request = chat_request()
    final_request = chat_request()
    request_gate = gate()
    error_body = {
        "error": {
            "code": "BACKEND_DB_TIMEOUT",
            "message": "timeout",
            "retryable": True,
            "details": None,
        }
    }
    try:
        assert request_gate.begin(retryable_request) is None
        request_gate.fail(
            retryable_request,
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
        assert request_gate.begin(retryable_request) is None

        assert request_gate.begin(final_request) is None
        final_body = {
            "error": {
                "code": "AI_PROCESSING_ERROR",
                "message": "failed",
                "retryable": False,
                "details": None,
            }
        }
        request_gate.fail(
            final_request,
            status_code=500,
            body=final_body,
            error_code="AI_PROCESSING_ERROR",
            retryable=False,
        )
        replay = request_gate.begin(final_request)
        assert replay is not None
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

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["message_id"] == request.message_id
        assert "trace_id" not in first.json()
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
        assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
        assert first.status_code == 200
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    finally:
        cleanup_request(request)


def test_sync_chat_http_exposes_request_and_conversation_lock_errors(monkeypatch) -> None:
    request = chat_request()
    competing = chat_request(conversation_id=request.conversation_id)

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
        assert request_gate.begin(request) is None
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
        assert in_progress.json()["error"]["code"] == "REQUEST_IN_PROGRESS"
        assert conversation_busy.status_code == 409
        assert conversation_busy.headers["Retry-After"] == "1"
        assert conversation_busy.json()["error"]["code"] == "CONVERSATION_BUSY"
    finally:
        cleanup_request(request)
        cleanup_request(competing)


def test_sync_chat_http_accepts_only_dedicated_agent_sync_token(monkeypatch) -> None:
    request = chat_request()

    class TokenSettings:
        agent_sync_api_token = "sync-secret"
        backend_api_token = "backend-secret"
        internal_api_token = "internal-secret"

        @staticmethod
        def require_agent_sync_api_token() -> str:
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
            backend_direction = client.post(
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
                headers={"Authorization": "Bearer sync-secret"},
            )

        expected_error = {
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
        assert backend_direction.status_code == 401
        assert backend_direction.json() == expected_error
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
            "error": {
                "code": "BACKEND_DB_UNAVAILABLE",
                "message": "The Backend read database is temporarily unavailable.",
                "retryable": True,
                "details": None,
            }
        }
        assert orchestrator.calls == 0
    finally:
        cleanup_request(request)
