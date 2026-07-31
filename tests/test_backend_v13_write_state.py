from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

from agent_app.integration.approval_state import (
    APPROVED,
    CONSUMED,
    EXPIRED,
    InternalApprovalError,
    InternalApprovalStore,
)
from agent_app.integration.backend_client import (
    BackendV13TransportError,
)
from shared.backend_v13_contracts import (
    RecordChangeResponse,
    RecordChangeResult,
)
from agent_app.integration.write_state import (
    COMPLETED,
    BackendWriteIdentity,
    BackendWriteStateStore,
    canonical_payload_hash,
)
from agent_app.integration import write_state
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import (
    AgentBackendWriteRequest,
    AgentPendingAction,
)
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
    backend_write_request_id,
)
from shared.tool_names import UPDATE_NUTRITION_MEAL_RECORD
from tests.helpers import build_agent_engine
from shared.time_utils import utc_now

NOW = datetime(2026, 7, 25, 17, 0, tzinfo=UTC)
PATIENT_ID = "patient_0000000000000001"
SOURCE_CHAT_REQUEST_ID = "req_0000000000000001"
SOURCE_MESSAGE_ID = "user_msg_0000000000000001"
APPROVAL_KEY = ""
ACTION_FINGERPRINT = ""


class VersionQueries:
    def __init__(self, version: int) -> None:
        self.version = version
        self.calls = 0

    def record_version(
        self,
        *,
        patient_id,
        resource_type,
        record_id,
        parent_record_id=None,
    ) -> int:
        self.calls += 1
        return self.version


class ResponseLostAfterCommitClient:
    def __init__(self) -> None:
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        raise BackendV13TransportError("response_lost_after_backend_commit")


class SuccessfulClient:
    def __init__(self) -> None:
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        return RecordChangeResponse(
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "unknown",
                parent_record_id=request.parent_record_id,
                version=(request.expected_version or 0) + 1,
            ),
            processed_at=NOW,
        )


class MustNotBeCalledClient:
    async def change_record(self, request):
        raise AssertionError(f"completed Backend response was not replayed: {request}")


class CapturingSuccessClient:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        self.started.set()
        await asyncio.wait_for(self.release.wait(), timeout=10)
        return RecordChangeResponse(
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "unknown",
                parent_record_id=request.parent_record_id,
                version=(request.expected_version or 0) + 1,
            ),
            processed_at=NOW,
        )


def invocation_context(
    *,
    requested_at: datetime = NOW,
) -> BackendWriteInvocationContext:
    return BackendWriteInvocationContext(
        source_chat_request_id=SOURCE_CHAT_REQUEST_ID,
        source_message_id=SOURCE_MESSAGE_ID,
        approval_key=APPROVAL_KEY,
        action_fingerprint=ACTION_FINGERPRINT,
        patient_id=PATIENT_ID,
        requested_at=requested_at,
    )


def state_store(tmp_path):
    engine, _cleanup = build_agent_engine(
        "agent_write_state"
    )
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    run_migrations(engine)
    return (
        engine,
        sessions,
        BackendWriteStateStore(
            sessions,
            retention_seconds=86_400,
        ),
        InternalApprovalStore(sessions),
    )


def register_internal_approval(
    approval_store: InternalApprovalStore,
    *,
    tool_call_id: str,
) -> None:
    global APPROVAL_KEY, ACTION_FINGERPRINT
    display = {
        "title": "식사 기록 수정",
        "question": "식사 기록을 수정할까요?",
        "action_label": "수정",
    }
    proposal = approval_store.prepare(
        patient_id=PATIENT_ID,
        source_chat_request_id="req_0000000000000090",
        source_message_id="user_msg_0000000000000090",
        trace_id="internal-trace-write-state",
        action_name=UPDATE_NUTRITION_MEAL_RECORD,
        tool_call_id=tool_call_id,
        arguments={"meal_id": "meal_0000000000000010"},
        display=display,
    )
    decision = approval_store.submit_response(
        patient_id=PATIENT_ID,
        current_user_message_id=SOURCE_MESSAGE_ID,
        current_source_chat_request_id=SOURCE_CHAT_REQUEST_ID,
        originating_user_message_id="user_msg_0000000000000090",
        submitted_value="수정",
        source_message={
            "message_type": "selection_box",
            "message": {
                "message_title": display["title"],
                "text": display["question"],
                "selections": ["수정", "취소"],
            },
        },
    )
    assert decision is not None and decision.kind == "approved"
    APPROVAL_KEY = decision.approval_key
    ACTION_FINGERPRINT = proposal.action_fingerprint


@pytest.mark.asyncio
async def test_backend_write_retry_reuses_persisted_version_after_restart_and_response_loss(
    tmp_path,
) -> None:
    engine, sessions, store, approval_store = state_store(tmp_path)
    register_internal_approval(
        approval_store,
        tool_call_id="tool-call-restart-001",
    )
    first_queries = VersionQueries(7)
    lost_client = ResponseLostAfterCommitClient()
    first_process = BackendSyncWriteTools(
        lost_client,
        first_queries,
        state_store=store,
        approval_store=approval_store,
    )
    arguments = {
        "meal_id": "meal_0000000000000010",
        "description": "민감한 점심 기록 정정",
    }

    with pytest.raises(
        BackendV13TransportError,
        match="response_lost_after_backend_commit",
    ):
        await first_process.execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            arguments,
            tool_call_id="tool-call-restart-001",
            context=invocation_context(),
        )

    assert len(lost_client.requests) == 1
    assert lost_client.requests[0].expected_version == 7
    assert first_queries.calls == 1
    with sessions() as session:
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == APPROVED
        assert approval.send_attempt_count == 1

    # Simulate Backend commit/version increment and a new AI Server process.
    restarted_queries = VersionQueries(8)
    success_client = SuccessfulClient()
    restarted_process = BackendSyncWriteTools(
        success_client,
        restarted_queries,
        state_store=BackendWriteStateStore(
            sessions,
            retention_seconds=86_400,
        ),
        approval_store=InternalApprovalStore(sessions),
    )
    result = await restarted_process.execute(
        UPDATE_NUTRITION_MEAL_RECORD,
        arguments,
        tool_call_id="tool-call-restart-001",
        context=invocation_context(
            requested_at=NOW + timedelta(seconds=5)
        ),
    )

    assert result.status == "success"
    assert len(success_client.requests) == 1
    assert success_client.requests[0].request_id == lost_client.requests[0].request_id
    assert success_client.requests[0].expected_version == 7
    assert success_client.requests[0].requested_at == (
        NOW + timedelta(seconds=5)
    )
    assert restarted_queries.calls == 0

    # A third process replays the AI-DB result without another Backend call.
    replay_process = BackendSyncWriteTools(
        MustNotBeCalledClient(),
        VersionQueries(99),
        state_store=BackendWriteStateStore(
            sessions,
            retention_seconds=86_400,
        ),
        approval_store=InternalApprovalStore(sessions),
    )
    replay = await replay_process.execute(
        UPDATE_NUTRITION_MEAL_RECORD,
        arguments,
        # A crash can make the LLM regenerate a different provider call ID.
        # The same source request/tool/arguments must still map to the first
        # canonical Backend request.
        tool_call_id="tool-call-regenerated-999",
        context=invocation_context(),
    )
    assert replay.response == result.response
    assert replay.idempotency_key == result.idempotency_key

    with sessions() as session:
        row = session.scalar(select(AgentBackendWriteRequest))
        assert row is not None
        persisted = " ".join(
            [
                row.request_id,
                row.source_chat_request_id,
                row.patient_id_hash,
                row.trusted_context_hash,
                row.tool_call_id,
                row.tool_name,
                row.argument_hash,
                row.response_json,
            ]
        )
        assert PATIENT_ID not in persisted
        assert "conversation-sensitive-001" not in persisted
        assert "민감한 점심 기록 정정" not in persisted
        assert row.expected_version == 7
        assert row.attempt_count == 2
        assert row.status == "COMPLETED"
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == CONSUMED
        assert approval.send_attempt_count == 2
    engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_ai_instances_share_canonical_request_and_preserve_terminal_response(
    tmp_path,
) -> None:
    engine, sessions, _, approval_store = state_store(tmp_path)
    register_internal_approval(
        approval_store,
        tool_call_id="tool-call-concurrent-provider-a",
    )
    versions = [VersionQueries(7), VersionQueries(8), VersionQueries(9)]
    success_client = CapturingSuccessClient()
    tools = [
        BackendSyncWriteTools(
            success_client,
            query,
            state_store=BackendWriteStateStore(
                sessions,
                retention_seconds=86_400,
            ),
            approval_store=InternalApprovalStore(sessions),
        )
        for query in versions
    ]
    arguments = {
        "meal_id": "meal_0000000000000010",
        "description": "concurrent update",
    }
    tool_call_ids = [
        "tool-call-concurrent-provider-a",
        "tool-call-concurrent-provider-b",
        "tool-call-concurrent-provider-c",
    ]

    successful_attempt = asyncio.create_task(
        tools[0].execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            arguments,
            tool_call_id=tool_call_ids[0],
            context=invocation_context(),
        )
    )
    await asyncio.wait_for(success_client.started.wait(), timeout=10)

    blocked = await asyncio.gather(
        tools[1].execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            arguments,
            tool_call_id=tool_call_ids[1],
            context=invocation_context(),
        ),
        tools[2].execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            arguments,
            tool_call_id=tool_call_ids[2],
            context=invocation_context(),
        ),
        return_exceptions=True,
    )

    assert all(isinstance(error, InternalApprovalError) for error in blocked)
    assert all(
        "internal_approval_already_sending" in str(error)
        for error in blocked
    )
    assert len(success_client.requests) == 1
    request = success_client.requests[0]
    canonical_candidates = {
        backend_write_request_id(
            source_chat_request_id=invocation_context().source_chat_request_id,
            tool_name=UPDATE_NUTRITION_MEAL_RECORD,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )
        for tool_call_id in tool_call_ids
    }
    assert request.request_id in canonical_candidates
    assert request.expected_version == 7
    assert [query.calls for query in versions] == [1, 0, 0]

    success_client.release.set()
    result = await successful_attempt
    assert result.status == "success"

    replay = await tools[1].execute(
        UPDATE_NUTRITION_MEAL_RECORD,
        arguments,
        tool_call_id=tool_call_ids[1],
        context=invocation_context(),
    )
    assert replay.response == result.response
    assert len(success_client.requests) == 1

    with sessions() as session:
        rows = session.scalars(select(AgentBackendWriteRequest)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.request_id == request.request_id
        assert row.expected_version == request.expected_version
        assert row.attempt_count == 1
        assert row.status == COMPLETED
        assert row.response_status == 200
        assert row.error_code == ""
        assert set(json.loads(row.response_json)) == {
            "request_id",
            "result",
            "processed_at",
        }
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == CONSUMED
        assert approval.send_attempt_count == 1
    engine.dispose()


@pytest.mark.asyncio
async def test_expired_internal_approval_blocks_backend_write(
    tmp_path,
) -> None:
    engine, sessions, store, approval_store = state_store(tmp_path)
    register_internal_approval(
        approval_store,
        tool_call_id="tool-call-expired-001",
    )
    with sessions() as session:
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        approval.action_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()

    tools = BackendSyncWriteTools(
        MustNotBeCalledClient(),
        VersionQueries(7),
        state_store=store,
        approval_store=approval_store,
    )
    with pytest.raises(
        InternalApprovalError,
        match="internal_approval_expired",
    ):
        await tools.execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            {
                "meal_id": "meal_0000000000000010",
                "description": "만료된 승인으로는 수정할 수 없음",
            },
            tool_call_id="tool-call-expired-001",
            context=invocation_context(),
        )

    with sessions() as session:
        approval = session.scalar(select(AgentPendingAction))
        assert approval is not None
        assert approval.status == EXPIRED
        assert approval.send_attempt_count == 0
    engine.dispose()


def test_concurrent_retryable_response_cannot_overwrite_terminal_state(
    tmp_path,
    monkeypatch,
) -> None:
    engine, _, store, _ = state_store(tmp_path)
    identity = BackendWriteIdentity(
        request_id="req_0000000000000010",
        source_chat_request_id="req_0000000000000011",
        patient_id_hash=canonical_payload_hash(
            {"patient_id": "patient_0000000000000001"}
        ),
        trusted_context_hash=canonical_payload_hash(
            {"patient_id": "patient_0000000000000001"}
        ),
        tool_call_id="tool-call-concurrent-terminal-001",
        tool_name=UPDATE_NUTRITION_MEAL_RECORD,
        argument_hash=canonical_payload_hash({"meal_id": 10}),
    )
    store.prepare(identity, expected_version=7)
    store.begin_attempt(identity.request_id)

    serialize_barrier = threading.Barrier(2)
    release_retryable = threading.Event()
    original_dump_json = write_state.dump_json

    def coordinated_dump_json(body):
        serialize_barrier.wait(timeout=10)
        if isinstance(body.get("error"), dict):
            assert release_retryable.wait(timeout=10)
        return original_dump_json(body)

    monkeypatch.setattr(write_state, "dump_json", coordinated_dump_json)
    terminal_body = {
        "request_id": identity.request_id,
        "result": {"version": 8},
        "processed_at": NOW.isoformat(),
    }
    retryable_body = {
        "request_id": identity.request_id,
        "error": {"code": "BACKEND_UNAVAILABLE", "retryable": True},
    }

    with ThreadPoolExecutor(max_workers=2) as executor:
        terminal_future = executor.submit(
            store.record_response,
            identity.request_id,
            status_code=200,
            body=terminal_body,
            terminal=True,
        )
        retryable_future = executor.submit(
            store.record_response,
            identity.request_id,
            status_code=503,
            body=retryable_body,
            terminal=False,
            error_code="BACKEND_UNAVAILABLE",
        )
        terminal_state = terminal_future.result(timeout=10)
        assert terminal_state.status == COMPLETED
        release_retryable.set()
        retryable_state = retryable_future.result(timeout=10)

    assert retryable_state.status == COMPLETED
    assert retryable_state.response_status == 200
    assert retryable_state.response_body == terminal_body
    after_transport = store.record_transport_failure(
        identity.request_id,
        error_code="BackendV13TransportError",
    )
    assert after_transport.status == COMPLETED
    assert after_transport.response_status == 200
    assert after_transport.response_body == terminal_body
    after_late_begin = store.begin_attempt(identity.request_id)
    assert after_late_begin.status == COMPLETED
    assert after_late_begin.response_status == 200
    assert after_late_begin.response_body == terminal_body
    engine.dispose()


def test_agent_migration_creates_backend_write_state_source_of_truth(
    tmp_path,
) -> None:
    engine, _, _, _ = state_store(tmp_path)
    columns = {
        column["name"]
        for column in inspect(engine).get_columns("agent_backend_write_requests")
    }
    assert {
        "request_id",
        "trusted_context_hash",
        "argument_hash",
        "expected_version",
        "status",
        "response_json",
        "expires_at",
    }.issubset(columns)
    unique_constraints = inspect(engine).get_unique_constraints(
        "agent_backend_write_requests"
    )
    assert any(
        constraint["column_names"] == ["request_id"]
        for constraint in unique_constraints
    )
    engine.dispose()
