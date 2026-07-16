from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agent_app.tool_names import UPDATE_MEDICATION_DOSE_EVENT_STATUS
from agent_app.tool_protocol import mcp_result_from_tool_result, tool_result_from_mcp_result
from agent_app.tool_runtime import ToolRuntime
from shared.json_utils import dump_json
from shared.schemas import (
    ConfirmedMutationExecutionRequest,
    MutationConfirmationPrepareRequest,
    ToolCallResult,
)
from system_app.models import AgentDecisionAudit, Base, ChatMessage, DoseEvent, MutationConfirmation, Notification
from system_app.services.mutation_confirmation_service import (
    APPLIED,
    CANCELLED,
    EXECUTING,
    FAILED,
    PENDING,
    STALE,
    SUPERSEDED,
    begin_mutation_resolution,
    execute_confirmed_mutation,
    prepare_mutation_confirmation,
    recover_expired_confirmations,
)
from shared.time_utils import utc_now


def _engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


def _seed_request(session: Session) -> tuple[DoseEvent, MutationConfirmationPrepareRequest]:
    now = datetime(2026, 4, 20, 9, 30)
    chat = ChatMessage(
        patient_id="demo-patient",
        role="user",
        sender_type="patient",
        category="multiturn_chat",
        content="I took my morning dose.",
        created_at=now,
    )
    session.add(chat)
    session.flush()
    notification = Notification(
        patient_id="demo-patient",
        notification_type="system_policy_request",
        title="request",
        body="request",
        visible_at=now,
        metadata_json=dump_json(
            {
                "event_type": "multiturn_chat",
                "request_message": chat.content,
                "chat_message_id": chat.id,
                "agent_conversation_id": "conversation-1",
            }
        ),
    )
    event = DoseEvent(
        patient_id="demo-patient",
        plan_id=1,
        schedule_id=1,
        medication_name="test-medication",
        slot_label="morning",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        status="scheduled",
    )
    session.add_all([notification, event])
    session.flush()
    request = MutationConfirmationPrepareRequest(
        patient_id="demo-patient",
        action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        tool_call_id="tool-call-1",
        arguments={"dose_event_id": event.id},
        trace_id="trace-prepare",
        source_event_type="medication_agent",
        request_context={
            "message": chat.content,
            "event_type": "multiturn_chat",
            "current_time": now.isoformat(),
            "callback_context": {
                "notification_id": notification.id,
                "conversation_id": "conversation-1",
            },
        },
    )
    session.commit()
    return event, request


def test_dose_mutation_waits_for_confirmation_and_commits_atomically():
    engine = _engine()
    with Session(engine) as session:
        event, request = _seed_request(session)
        event_id = event.id
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        assert prepared.confirmation_required is True
        assert session.get(DoseEvent, event_id).status == "scheduled"

        row = begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()
        assert row.status == EXECUTING

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-confirmed",
                source_event_type="medication_agent",
            ),
        )
        assert result.status == APPLIED
        assert session.get(DoseEvent, event_id).status == "taken"
        session.rollback()

    with Session(engine) as session:
        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assert row.status == EXECUTING
        assert session.get(DoseEvent, event_id).status == "scheduled"


def test_dose_mutation_becomes_stale_when_snapshot_changes():
    engine = _engine()
    with Session(engine) as session:
        event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        event.status = "missed"
        session.commit()

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-confirmed",
                source_event_type="medication_agent",
            ),
        )
        session.commit()
        assert result.status == STALE
        assert session.get(DoseEvent, event.id).status == "missed"


def test_duplicate_prepare_and_execution_are_idempotent():
    engine = _engine()
    with Session(engine) as session:
        event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()

        retry_request = request.model_copy(update={"trace_id": "trace-retry"})
        duplicate = prepare_mutation_confirmation(session, retry_request)
        session.commit()
        assert duplicate.confirmation_id == prepared.confirmation_id
        assert session.query(MutationConfirmation).count() == 1

        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()
        execution_request = ConfirmedMutationExecutionRequest(
            confirmation_id=prepared.confirmation_id,
            action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            action_fingerprint=prepared.action_fingerprint,
            trace_id="trace-confirmed",
            source_event_type="medication_agent",
        )
        first = execute_confirmed_mutation(session, prepared.confirmation_id, execution_request)
        session.commit()
        audit_count = session.query(AgentDecisionAudit).count()

        second = execute_confirmed_mutation(session, prepared.confirmation_id, execution_request)
        session.commit()
        assert first.status == second.status == APPLIED
        assert session.get(DoseEvent, event.id).status == "taken"
        assert session.query(AgentDecisionAudit).count() == audit_count


def test_new_confirmation_is_created_when_applied_state_is_no_longer_current():
    engine = _engine()
    with Session(engine) as session:
        event, request = _seed_request(session)
        first = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            first.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()
        execute_confirmed_mutation(
            session,
            first.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=first.confirmation_id,
                action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                action_fingerprint=first.action_fingerprint,
                trace_id="trace-first-confirmed",
                source_event_type="medication_agent",
            ),
        )
        session.commit()
        assert event.status == "taken"

        event.status = "scheduled"
        event.taken_at = None
        session.commit()

        second = prepare_mutation_confirmation(
            session,
            request.model_copy(update={"trace_id": "trace-new-request"}),
        )
        session.commit()

        assert second.confirmation_required is True
        assert second.confirmation_id != first.confirmation_id
        assert session.query(MutationConfirmation).count() == 2
        assert event.status == "scheduled"


def test_late_proposal_is_superseded_after_new_general_message():
    engine = _engine()
    with Session(engine) as session:
        _event, request = _seed_request(session)
        session.add(
            ChatMessage(
                patient_id="demo-patient",
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                content="Start a new request.",
                created_at=datetime(2026, 4, 20, 9, 31),
            )
        )
        session.commit()

        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        assert prepared.confirmation_required is False
        assert prepared.status == SUPERSEDED


def test_cancel_keeps_domain_state_and_expired_execution_fails():
    engine = _engine()
    with Session(engine) as session:
        event, request = _seed_request(session)
        cancelled = prepare_mutation_confirmation(session, request)
        session.commit()
        row = begin_mutation_resolution(
            session,
            cancelled.confirmation_id,
            "cancel",
            patient_id="demo-patient",
        )
        session.commit()
        assert row.status == CANCELLED
        assert session.get(DoseEvent, event.id).status == "scheduled"

        retry_request = request.model_copy(
            update={
                "trace_id": "trace-second-action",
                "arguments": {"dose_event_id": event.id, "taken_at": "2026-04-20T09:35:00"},
            }
        )
        pending = prepare_mutation_confirmation(session, retry_request)
        session.commit()
        row = begin_mutation_resolution(
            session,
            pending.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        row.execution_started_at = utc_now() - timedelta(minutes=3)
        session.commit()

        assert recover_expired_confirmations(session, "demo-patient") == 1
        session.commit()
        assert row.status == FAILED
        assert row.error_message == "mutation_confirmation_execution_lease_expired"
        assert session.get(DoseEvent, event.id).status == "scheduled"


class _ConfirmationExecutor:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
        self.calls.append(tool_call)
        return ToolCallResult(
            tool_name=tool_call["name"],
            status="confirmation_required",
            response={"mutation_confirmation": {"confirmation_id": "confirmation-1"}},
        )


def test_tool_runtime_stops_after_first_confirmation_required():
    executor = _ConfirmationExecutor()
    runtime = ToolRuntime(executor)
    calls = [
        {"id": "one", "name": UPDATE_MEDICATION_DOSE_EVENT_STATUS, "arguments": {"dose_event_id": 1}},
        {"id": "two", "name": "get_medication_dose_status", "arguments": {}},
    ]

    executed, results = asyncio.run(
        runtime.execute(
            calls,
            trace_id="trace-runtime",
            source_event_type="medication_agent",
            payload={"patient_id": "demo-patient"},
        )
    )

    assert executed == calls
    assert len(executor.calls) == 1
    assert [result.status for result in results] == ["confirmation_required", "skipped"]
    assert results[1].error == "blocked_by_pending_confirmation"


def test_mcp_protocol_preserves_confirmation_required_status():
    original = ToolCallResult(
        tool_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        status="confirmation_required",
        response={"mutation_confirmation": {"confirmation_id": "confirmation-1"}},
    )

    restored = tool_result_from_mcp_result(
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        mcp_result_from_tool_result(original),
    )

    assert restored.status == "confirmation_required"
    assert restored.response == original.response
