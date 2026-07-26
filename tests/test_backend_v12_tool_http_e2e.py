from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest

from agent_app.integration.backend_client import BackendV12Client
from agent_app.integration.write_state import BackendWriteStateStore
from agent_app.persistence.migrations import run_migrations as run_agent_migrations
from agent_app.persistence.models import Base as AgentBase
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
)
from agent_app.tools.names import UPDATE_MEDICATION_DOSE_EVENT_STATUS
from shared.db import create_session_factory
from system_app import main as system_main
from system_app.db import SessionLocal
from system_app.models import ChatMessage, DoseEvent, DoseSchedule, MedicationPlan

NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class CountingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.ASGITransport(app=system_main.app)
        self.calls: list[tuple[str, str]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.mark.asyncio
async def test_ai_write_tool_reads_version_then_calls_real_backend_http_boundary(
    tmp_path,
) -> None:
    suffix = uuid4().hex
    patient_id = f"patient-tool-e2e-{suffix}"
    conversation_id = f"conversation-tool-e2e-{suffix}"
    source_request_id = f"source-tool-e2e-{suffix}"
    scheduled_for = NOW.replace(tzinfo=None) + timedelta(
        microseconds=int(suffix[:10], 16)
    )
    with SessionLocal() as session:
        plan = MedicationPlan(
            patient_id=patient_id,
            medication_name="테스트약",
            dosage="1정",
            instructions="",
            start_date=scheduled_for.date(),
            end_date=scheduled_for.date(),
            active=True,
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(
            plan_id=plan.id,
            slot_label="저녁",
            scheduled_time=scheduled_for.strftime("%H:%M"),
        )
        session.add(schedule)
        session.flush()
        confirmation = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="방금 복용한 것으로 기록해줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        event = DoseEvent(
            patient_id=patient_id,
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name="테스트약",
            slot_label="저녁",
            scheduled_for=scheduled_for,
            status="scheduled",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add_all([confirmation, event])
        session.commit()
        confirmation_id = confirmation.id
        confirmation_public_id = confirmation.public_id
        event_id = event.id
        event_public_id = event.public_id

    state_engine, state_sessions = create_session_factory(
        f"sqlite:///{(tmp_path / 'agent-write-e2e.db').as_posix()}"
    )
    AgentBase.metadata.create_all(state_engine)
    run_agent_migrations(state_engine)
    state_store = BackendWriteStateStore(
        state_sessions,
        retention_seconds=86_400,
    )
    queries = BackendQueryTools(os.environ["SYSTEM_DATABASE_URL"])
    transport = CountingASGITransport()
    client = BackendV12Client(
        base_url="http://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="pytest-backend-api-token",
        max_retries=0,
        transport=transport,
    )
    tools = BackendSyncWriteTools(
        client,
        queries,
        state_store=state_store,
    )
    context = BackendWriteInvocationContext(
        source_chat_request_id=source_request_id,
        source_message_id=confirmation_public_id,
        conversation_id=conversation_id,
        patient_id=patient_id,
        requested_at=NOW,
    )

    first = await tools.execute(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {"dose_event_id": event_id},
        tool_call_id="provider-call-before-restart",
        context=context,
    )

    assert first.status == "success"
    assert transport.calls == [("POST", "/agent/sync/record-change")]
    assert first.response["request_id"] == first.idempotency_key
    assert first.response["result"]["record_id"] == event_public_id
    assert first.response["result"]["version"] == 2

    # A new AI process and regenerated provider call ID must replay the AI-DB
    # terminal response without another HTTP mutation.
    restarted_tools = BackendSyncWriteTools(
        client,
        BackendQueryTools(os.environ["SYSTEM_DATABASE_URL"]),
        state_store=BackendWriteStateStore(
            state_sessions,
            retention_seconds=86_400,
        ),
    )
    replay = await restarted_tools.execute(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {"dose_event_id": event_id},
        tool_call_id="provider-call-after-restart",
        context=context,
    )

    assert replay.response == first.response
    assert replay.idempotency_key == first.idempotency_key
    assert transport.calls == [("POST", "/agent/sync/record-change")]
    with SessionLocal() as session:
        changed = session.get(DoseEvent, event_id)
        assert changed is not None
        assert changed.patient_id == patient_id
        assert changed.status == "taken"
        assert changed.version == 2

    queries.engine.dispose()
    restarted_tools.backend_queries.engine.dispose()
    state_engine.dispose()
