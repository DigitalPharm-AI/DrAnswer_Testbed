from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.orm import sessionmaker

from agent_app.integration.approval_state import InternalApprovalStore
from agent_app.integration.backend_client import BackendV13Client
from agent_app.integration.write_state import BackendWriteStateStore
from agent_app.persistence.migrations import run_migrations as run_agent_migrations
from agent_app.tools.backend_query import BackendQueryTools
from agent_app.tools.backend_write import (
    BackendSyncWriteTools,
    BackendWriteInvocationContext,
)
from shared.tool_names import UPDATE_MEDICATION_DOSE_EVENT_STATUS
from system_app import main as system_main
from system_app.db import SessionLocal
from system_app.db import engine as system_engine
from system_app.models import (
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
)
from system_app.services.backend_v13_service import (
    RECORD_CHANGE_PATH,
    BackendRequestConflict,
    BackendRequestGate,
)
from tests.helpers import (
    build_agent_engine,
    build_backend_reader_url,
)

NOW = datetime(2026, 7, 25, 18, 0, tzinfo=UTC)


class CountingASGITransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.ASGITransport(app=system_main.app)
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        self.bodies.append(json.loads((await request.aread()).decode("utf-8")))
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.mark.asyncio
async def test_approved_ai_write_uses_bound_version_and_real_backend_boundary(
    tmp_path,
) -> None:
    suffix = uuid4().hex
    patient_id = f"patient_{suffix[16:32]}"
    source_request_id = f"req_{suffix[:16]}"
    write_request_id = f"req_{suffix[8:24]}"
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
        session.flush()
        approval_card = ChatMessage(
            patient_id=patient_id,
            ai_request_id=f"req_{uuid4().hex[:16]}",
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type="selection_box",
            content="복약 완료를 기록할까요?",
            message_payload_json=json.dumps(
                {
                    "message_title": "복약 완료 기록",
                    "text": "복약 완료를 기록할까요?",
                    "selections": ["기록", "취소"],
                },
                ensure_ascii=False,
            ),
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(approval_card)
        session.flush()
        confirmation.reply_to_message_id = approval_card.id
        confirmation.message_type = "selection_box"
        confirmation.content = "기록"
        session.commit()
        confirmation_public_id = confirmation.public_id
        event_id = event.id
        event_public_id = event.public_id

    state_engine, _cleanup = build_agent_engine(
        "agent_write_e2e"
    )
    state_sessions = sessionmaker(
        bind=state_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    run_agent_migrations(state_engine)
    state_store = BackendWriteStateStore(
        state_sessions,
        retention_seconds=86_400,
    )
    approval_store = InternalApprovalStore(state_sessions)
    display = {
        "title": "복약 완료 기록",
        "question": "복약 완료를 기록할까요?",
        "action_label": "기록",
    }
    origin_message_id = f"user_msg_{uuid4().hex[:16]}"
    proposal = approval_store.prepare(
        patient_id=patient_id,
        source_chat_request_id=f"req_{uuid4().hex[:16]}",
        source_message_id=origin_message_id,
        trace_id=f"trace-tool-e2e-{suffix}",
        action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        tool_call_id="provider-call-before-restart",
        arguments={"dose_event_id": event_public_id},
        display=display,
    )
    decision = approval_store.submit_response(
        patient_id=patient_id,
        current_user_message_id=confirmation_public_id,
        current_source_chat_request_id=source_request_id,
        originating_user_message_id=origin_message_id,
        submitted_value="기록",
        source_message={
            "message_type": "selection_box",
            "message": {
                "message_title": display["title"],
                "text": display["question"],
                "selections": ["기록", "취소"],
            },
        },
    )
    assert decision is not None and decision.kind == "approved"
    reader_url, reader_cleanup = build_backend_reader_url(
        system_engine,
        "backend_tool_e2e_reader",
    )
    queries = BackendQueryTools(reader_url)
    transport = CountingASGITransport()
    client = BackendV13Client(
        base_url="http://backend.test",
        record_change_path="/agent/sync/record-change",
        notification_policy_change_path="/agent/sync/notification-policy-change",
        bearer_token="pytest-agent-sync-token",
        max_retries=0,
        transport=transport,
    )
    tools = BackendSyncWriteTools(
        client,
        queries,
        state_store=state_store,
        approval_store=approval_store,
    )
    context = BackendWriteInvocationContext(
        source_chat_request_id=source_request_id,
        source_message_id=confirmation_public_id,
        approval_key=decision.approval_key,
        action_fingerprint=proposal.action_fingerprint,
        patient_id=patient_id,
        requested_at=NOW,
    )

    first = await tools.execute(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {"dose_event_id": event_public_id},
        tool_call_id="provider-call-before-restart",
        context=context,
        request_id_override=write_request_id,
        expected_version_override=1,
    )

    assert first.status == "success"
    assert transport.calls == [("POST", "/agent/sync/record-change")]
    assert "approval_key" not in transport.bodies[0]
    assert "source_trace_id" not in json.dumps(transport.bodies[0])
    assert first.response["request_id"] == first.idempotency_key
    assert first.response["result"]["record_id"] == event_public_id
    assert first.response["result"]["version"] == 2

    # A new AI process and regenerated provider call ID must replay the AI-DB
    # terminal response without another HTTP mutation.
    restarted_tools = BackendSyncWriteTools(
        client,
        BackendQueryTools(reader_url),
        state_store=BackendWriteStateStore(
            state_sessions,
            retention_seconds=86_400,
        ),
        approval_store=InternalApprovalStore(state_sessions),
    )
    replay = await restarted_tools.execute(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        {"dose_event_id": event_public_id},
        tool_call_id="provider-call-after-restart",
        context=context,
        request_id_override=write_request_id,
        expected_version_override=1,
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
    reader_cleanup()
    _cleanup()


def test_backend_idempotency_ignores_only_requested_at() -> None:
    suffix = uuid4().hex
    request_id = f"req_{suffix[:16]}"
    base_payload = {
        "request_id": request_id,
        "patient_id": f"patient_{suffix[8:24]}",
        "resource_type": "medication_dose_event",
        "operation": "update",
        "record_id": f"dose_{suffix[16:32]}",
        "payload": {"status": "taken"},
        "requested_at": "2026-07-25T18:00:00Z",
    }
    response_body = {
        "request_id": request_id,
        "result": {"status": "applied"},
    }
    gate = BackendRequestGate()

    with SessionLocal() as session:
        assert (
            gate.begin(
                session,
                api_path=RECORD_CHANGE_PATH,
                request_id=request_id,
                payload=base_payload,
            )
            is None
        )
        gate.complete(
            session,
            api_path=RECORD_CHANGE_PATH,
            request_id=request_id,
            status_code=200,
            body=response_body,
        )
        session.commit()

    later_retry = {
        **base_payload,
        "requested_at": "2026-07-25T18:00:05Z",
    }
    with SessionLocal() as session:
        replay = gate.begin(
            session,
            api_path=RECORD_CHANGE_PATH,
            request_id=request_id,
            payload=later_retry,
        )
        assert replay is not None
        assert replay.status_code == 200
        assert replay.body == response_body

    changed_business_payload = {
        **later_retry,
        "payload": {"status": "scheduled"},
    }
    with SessionLocal() as session:
        with pytest.raises(
            BackendRequestConflict,
            match="different request body",
        ):
            gate.begin(
                session,
                api_path=RECORD_CHANGE_PATH,
                request_id=request_id,
                payload=changed_business_payload,
            )
