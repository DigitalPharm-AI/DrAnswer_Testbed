from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select

from agent_app.integration.backend_client import BackendV12TransportError
from agent_app.integration.contracts import (
    ContractError,
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
from agent_app.persistence.models import AgentBackendWriteRequest, Base
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
    backend_write_request_id,
)
from agent_app.tools.names import UPDATE_NUTRITION_MEAL_RECORD
from shared.db import create_session_factory

NOW = datetime(2026, 7, 25, 17, 0, tzinfo=UTC)


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
        raise BackendV12TransportError("response_lost_after_backend_commit")


class SuccessfulClient:
    def __init__(self) -> None:
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        return RecordChangeResponse(
            success=True,
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "unknown",
                parent_record_id=request.parent_record_id,
                version=(request.expected_version or 0) + 1,
            ),
            error=None,
            processed_at=NOW,
        )


class MustNotBeCalledClient:
    async def change_record(self, request):
        raise AssertionError(f"completed Backend response was not replayed: {request}")


class CoordinatedVersionQueries:
    def __init__(self, version: int, barrier: threading.Barrier) -> None:
        self.version = version
        self.barrier = barrier
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
        self.barrier.wait(timeout=10)
        return self.version


class AsyncCallBarrier:
    def __init__(self, parties: int) -> None:
        self.parties = parties
        self.arrived = 0
        self.lock = asyncio.Lock()
        self.ready = asyncio.Event()

    async def wait(self) -> None:
        async with self.lock:
            self.arrived += 1
            if self.arrived == self.parties:
                self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=10)


class CapturingSuccessClient:
    def __init__(self, call_barrier: AsyncCallBarrier) -> None:
        self.call_barrier = call_barrier
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        await self.call_barrier.wait()
        return RecordChangeResponse(
            success=True,
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "unknown",
                parent_record_id=request.parent_record_id,
                version=(request.expected_version or 0) + 1,
            ),
            error=None,
            processed_at=NOW,
        )


class CapturingLateRetryableClient:
    def __init__(
        self,
        terminal_stored: asyncio.Event,
        call_barrier: AsyncCallBarrier,
    ) -> None:
        self.terminal_stored = terminal_stored
        self.call_barrier = call_barrier
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        await self.call_barrier.wait()
        await self.terminal_stored.wait()
        return RecordChangeResponse(
            success=False,
            request_id=request.request_id,
            result=None,
            error=ContractError(
                code="BACKEND_UNAVAILABLE",
                message="retry later",
                retryable=True,
            ),
            processed_at=NOW,
        )


class CapturingLateTransportFailureClient:
    def __init__(
        self,
        terminal_stored: asyncio.Event,
        call_barrier: AsyncCallBarrier,
    ) -> None:
        self.terminal_stored = terminal_stored
        self.call_barrier = call_barrier
        self.requests = []

    async def change_record(self, request):
        self.requests.append(request)
        await self.call_barrier.wait()
        await self.terminal_stored.wait()
        raise BackendV12TransportError("late_transport_failure")


def invocation_context() -> BackendWriteInvocationContext:
    return BackendWriteInvocationContext(
        source_chat_request_id="chat-request-restart-001",
        source_message_id="501",
        conversation_id="conversation-sensitive-001",
        patient_id="patient-sensitive-001",
        requested_at=NOW,
    )


def state_store(tmp_path):
    engine, sessions = create_session_factory(
        f"sqlite:///{(tmp_path / 'agent-write-state.db').as_posix()}"
    )
    Base.metadata.create_all(engine)
    run_migrations(engine)
    return engine, sessions, BackendWriteStateStore(
        sessions,
        retention_seconds=86_400,
    )


@pytest.mark.asyncio
async def test_backend_write_retry_reuses_persisted_version_after_restart_and_response_loss(
    tmp_path,
) -> None:
    engine, sessions, store = state_store(tmp_path)
    first_queries = VersionQueries(7)
    lost_client = ResponseLostAfterCommitClient()
    first_process = BackendSyncWriteTools(
        lost_client,
        first_queries,
        state_store=store,
    )
    arguments = {
        "meal_id": 10,
        "description": "민감한 점심 기록 정정",
    }

    with pytest.raises(
        BackendV12TransportError,
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
    )
    result = await restarted_process.execute(
        UPDATE_NUTRITION_MEAL_RECORD,
        arguments,
        tool_call_id="tool-call-restart-001",
        context=invocation_context(),
    )

    assert result.status == "success"
    assert len(success_client.requests) == 1
    assert success_client.requests[0].request_id == lost_client.requests[0].request_id
    assert success_client.requests[0].expected_version == 7
    assert restarted_queries.calls == 0

    # A third process replays the AI-DB result without another Backend call.
    replay_process = BackendSyncWriteTools(
        MustNotBeCalledClient(),
        VersionQueries(99),
        state_store=BackendWriteStateStore(
            sessions,
            retention_seconds=86_400,
        ),
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
                row.conversation_id_hash,
                row.trusted_context_hash,
                row.tool_call_id,
                row.tool_name,
                row.argument_hash,
                row.response_json,
            ]
        )
        assert "patient-sensitive-001" not in persisted
        assert "conversation-sensitive-001" not in persisted
        assert "민감한 점심 기록 정정" not in persisted
        assert row.expected_version == 7
        assert row.attempt_count == 2
        assert row.status == "COMPLETED"
    engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_ai_instances_share_canonical_request_and_preserve_terminal_response(
    tmp_path,
) -> None:
    engine, sessions, _ = state_store(tmp_path)
    version_barrier = threading.Barrier(3)
    terminal_stored = asyncio.Event()
    call_barrier = AsyncCallBarrier(3)
    versions = [
        CoordinatedVersionQueries(7, version_barrier),
        CoordinatedVersionQueries(8, version_barrier),
        CoordinatedVersionQueries(9, version_barrier),
    ]
    success_client = CapturingSuccessClient(call_barrier)
    retryable_client = CapturingLateRetryableClient(
        terminal_stored,
        call_barrier,
    )
    transport_client = CapturingLateTransportFailureClient(
        terminal_stored,
        call_barrier,
    )
    clients = [success_client, retryable_client, transport_client]
    tools = [
        BackendSyncWriteTools(
            client,
            query,
            state_store=BackendWriteStateStore(
                sessions,
                retention_seconds=86_400,
            ),
        )
        for client, query in zip(clients, versions, strict=True)
    ]
    arguments = {
        "meal_id": 10,
        "description": "concurrent update",
    }
    tool_call_ids = [
        "tool-call-concurrent-provider-a",
        "tool-call-concurrent-provider-b",
        "tool-call-concurrent-provider-c",
    ]

    async def successful_attempt():
        result = await tools[0].execute(
            UPDATE_NUTRITION_MEAL_RECORD,
            arguments,
            tool_call_id=tool_call_ids[0],
            context=invocation_context(),
        )
        terminal_stored.set()
        return result

    outcomes = await asyncio.gather(
        successful_attempt(),
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

    assert outcomes[0].status == "success"
    assert outcomes[1].status == "success"
    assert isinstance(outcomes[2], BackendV12TransportError)
    requests = [
        success_client.requests[0],
        retryable_client.requests[0],
        transport_client.requests[0],
    ]
    assert len({request.request_id for request in requests}) == 1
    canonical_candidates = {
        backend_write_request_id(
            source_chat_request_id=invocation_context().source_chat_request_id,
            tool_name=UPDATE_NUTRITION_MEAL_RECORD,
            tool_call_id=tool_call_id,
            arguments=arguments,
        )
        for tool_call_id in tool_call_ids
    }
    assert requests[0].request_id in canonical_candidates
    assert len({request.expected_version for request in requests}) == 1
    assert requests[0].expected_version in {7, 8, 9}
    assert [query.calls for query in versions] == [1, 1, 1]

    with sessions() as session:
        rows = session.scalars(select(AgentBackendWriteRequest)).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.request_id == requests[0].request_id
        assert row.expected_version == requests[0].expected_version
        assert row.attempt_count == 3
        assert row.status == COMPLETED
        assert row.response_status == 200
        assert row.error_code == ""
        assert json.loads(row.response_json)["success"] is True
    engine.dispose()


def test_concurrent_retryable_response_cannot_overwrite_terminal_state(
    tmp_path,
    monkeypatch,
) -> None:
    engine, _, store = state_store(tmp_path)
    identity = BackendWriteIdentity(
        request_id="write-concurrent-terminal-001",
        source_chat_request_id="chat-concurrent-terminal-001",
        conversation_id_hash=canonical_payload_hash({"conversation_id": "conversation-1"}),
        trusted_context_hash=canonical_payload_hash({"patient_id": "patient-1"}),
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
        if body.get("success") is False:
            assert release_retryable.wait(timeout=10)
        return original_dump_json(body)

    monkeypatch.setattr(write_state, "dump_json", coordinated_dump_json)
    terminal_body = {
        "success": True,
        "request_id": identity.request_id,
        "result": {"version": 8},
    }
    retryable_body = {
        "success": False,
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
        error_code="BackendV12TransportError",
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
    engine, _, _ = state_store(tmp_path)
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
