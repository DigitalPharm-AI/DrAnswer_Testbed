from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from agent_app.async_worker import _continuation_payload
from agent_app.confirmation_actions import ConfirmationActionRegistry
from agent_app.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from agent_app.tool_protocol import mcp_result_from_tool_result, tool_result_from_mcp_result
from agent_app.tool_runtime import ToolRuntime
from shared.json_utils import dump_json
from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentResponse,
    ConfirmedMutationExecutionRequest,
    MultiturnChatRequest,
    MutationConfirmationPrepareRequest,
    ToolCallResult,
)
from shared.time_utils import utc_now
from system_app.models import (
    AgentDecisionAudit,
    Base,
    ChatMessage,
    DailyNutritionCheck,
    DoseEvent,
    MutationConfirmation,
    Notification,
    NutritionFood,
    NutritionMeal,
    NutritionOntologyNode,
    NutritionPatientPreferenceTriple,
)
from system_app.services.agent_async_callback_service import process_async_chat_result_callback
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
from system_app.services.nutrition_preference_service import record_preference_fact
from system_app.services.system_request_service import build_async_continuation_request


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


def _seed_preference_request(session: Session) -> MutationConfirmationPrepareRequest:
    now = datetime(2026, 4, 20, 18, 30)
    chat = ChatMessage(
        patient_id="demo-patient",
        role="user",
        sender_type="patient",
        category="multiturn_chat",
        content="What should I eat for dinner? I am allergic to apples.",
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
                "agent_conversation_id": "conversation-preference",
            }
        ),
    )
    session.add(notification)
    session.flush()
    request = MutationConfirmationPrepareRequest(
        patient_id="demo-patient",
        action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
        tool_call_id="tool-call-preference",
        arguments={
            "patient_id": "untrusted-patient",
            "predicate": "allergic_to",
            "object_label": "apple",
            "object_type": "ingredient",
            "evidence_text": chat.content,
        },
        trace_id="trace-preference-prepare",
        source_event_type="nutrition_management_agent",
        request_context={
            "message": chat.content,
            "event_type": "multiturn_chat",
            "current_time": now.isoformat(),
            "callback_context": {
                "notification_id": notification.id,
                "conversation_id": "conversation-preference",
            },
        },
    )
    session.commit()
    return request




def _seed_nutrition_crud_request(
    session: Session,
    action_name: str,
    arguments: dict,
    *,
    trace_suffix: str,
) -> MutationConfirmationPrepareRequest:
    now = datetime(2026, 4, 20, 12, 30)
    chat = ChatMessage(
        patient_id="demo-patient",
        role="user",
        sender_type="patient",
        category="multiturn_chat",
        content=f"nutrition mutation {trace_suffix}",
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
                "agent_conversation_id": f"conversation-{trace_suffix}",
            }
        ),
    )
    session.add(notification)
    session.flush()
    request = MutationConfirmationPrepareRequest(
        patient_id="demo-patient",
        action_name=action_name,
        tool_call_id=f"tool-call-{trace_suffix}",
        arguments=arguments,
        trace_id=f"trace-{trace_suffix}",
        source_event_type="nutrition_management_agent",
        request_context={
            "message": chat.content,
            "event_type": "multiturn_chat",
            "current_time": now.isoformat(),
            "callback_context": {
                "notification_id": notification.id,
                "conversation_id": f"conversation-{trace_suffix}",
            },
        },
    )
    session.commit()
    return request


def _confirm_and_execute_nutrition_mutation(
    session: Session,
    prepared,
    action_name: str,
):
    begin_mutation_resolution(
        session,
        prepared.confirmation_id,
        "confirm",
        patient_id="demo-patient",
    )
    session.commit()
    result = execute_confirmed_mutation(
        session,
        prepared.confirmation_id,
        ConfirmedMutationExecutionRequest(
            confirmation_id=prepared.confirmation_id,
            action_name=action_name,
            action_fingerprint=prepared.action_fingerprint,
            trace_id=f"confirmed-{prepared.confirmation_id}",
            source_event_type="nutrition_management_agent",
        ),
    )
    session.commit()
    return result


def _seed_confirmation_reply_notification(
    session: Session,
    confirmation_id: str,
    *,
    message: str,
) -> Notification:
    notification = Notification(
        patient_id="demo-patient",
        notification_type="system_policy_request",
        title="confirmation reply",
        body=message,
        visible_at=datetime(2026, 4, 20, 9, 31),
        metadata_json=dump_json(
            {
                "event_type": "multiturn_chat",
                "request_message": message,
                "status": "sent",
                "pending_mutation_confirmation_id": confirmation_id,
            }
        ),
    )
    session.add(notification)
    session.flush()
    return notification


def _confirmation_reply_callback(
    notification: Notification,
    *,
    intent: str,
    message: str,
) -> AgentAsyncChatResultRequest:
    return AgentAsyncChatResultRequest(
        request_id=f"chat-continuation-{intent}",
        notification_id=notification.id,
        event_type="multiturn_chat",
        message=message,
        response=AgentResponse(
            trace_id=f"trace-confirmation-reply-{intent}",
            agent_name="multiturn_chat_agent",
            prompt_version_id="test",
            decision_type=(
                "system_guidance"
                if intent in {"new_request", "revise"}
                else "mutation_confirmation_reply"
            ),
            structured_payload={
                "routing_mode": "mutation_confirmation_reply",
                "mutation_confirmation_reply": {"intent": intent},
                "tool_calls": [],
                "tool_results": [],
            },
            human_summary=f"supervisor response for {intent}",
        ),
        idempotency_key=f"confirmation-reply-{intent}-once",
    )


def test_nutrition_preference_mutation_waits_for_confirmation_and_commits_atomically():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_preference_request(session)
        assert ConfirmationActionRegistry.requires_confirmation(UPSERT_NUTRITION_PREFERENCE_FACT)

        prepared = prepare_mutation_confirmation(session, request)
        session.commit()

        assert prepared.confirmation_required is True
        assert prepared.status == PENDING
        assert prepared.display["title"] == "\uc601\uc591 \uc81c\uc57d \uc815\ubcf4 \uae30\ub85d"
        assert prepared.display["question"] == "apple \uc54c\ub808\ub974\uae30\ub97c \uc601\uc591 \uc81c\uc57d \uc815\ubcf4\ub85c \uae30\ub85d\ud560\uae4c\uc694?"
        assert session.query(NutritionOntologyNode).count() == 0
        assert session.query(NutritionPatientPreferenceTriple).count() == 0

        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-preference-confirmed",
                source_event_type="nutrition_management_agent",
            ),
        )

        assert result.status == APPLIED
        fact = session.query(NutritionPatientPreferenceTriple).one()
        node = session.get(NutritionOntologyNode, fact.object_node_id)
        assert fact.patient_id == "demo-patient"
        assert fact.predicate == "allergic_to"
        assert fact.safety_level == "hard"
        assert node.node_key == "ingredient:apple"
        session.rollback()

    with Session(engine) as session:
        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assert row.status == EXECUTING
        assert session.query(NutritionOntologyNode).count() == 0
        assert session.query(NutritionPatientPreferenceTriple).count() == 0


def test_nutrition_preference_confirmation_applies_once_and_detects_existing_fact():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_preference_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()
        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-preference-confirmed",
                source_event_type="nutrition_management_agent",
            ),
        )
        session.commit()

        duplicate = prepare_mutation_confirmation(
            session,
            request.model_copy(update={"trace_id": "trace-preference-repeat"}),
        )
        session.commit()

        assert result.status == APPLIED
        assert duplicate.confirmation_required is False
        assert duplicate.status == "already_applied"
        assert duplicate.execution_result["fact"]["object_label"] == "apple"
        assert session.query(MutationConfirmation).count() == 1
        assert session.query(NutritionPatientPreferenceTriple).count() == 1


def test_cancelled_nutrition_preference_confirmation_keeps_domain_state_unchanged():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_preference_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()

        row = begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "cancel",
            patient_id="demo-patient",
        )
        session.commit()

        assert row.status == CANCELLED
        assert session.query(NutritionOntologyNode).count() == 0
        assert session.query(NutritionPatientPreferenceTriple).count() == 0


def test_nutrition_preference_confirmation_becomes_stale_when_target_changes():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_preference_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()

        record_preference_fact(
            session,
            patient_id="demo-patient",
            predicate="allergic_to",
            object_label="apple",
            object_type="ingredient",
            strength=0.4,
            evidence_text="concurrent change",
        )
        session.commit()

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-preference-stale",
                source_event_type="nutrition_management_agent",
            ),
        )
        session.commit()

        fact = session.query(NutritionPatientPreferenceTriple).one()
        assert result.status == STALE
        assert fact.strength == 0.4



@pytest.mark.parametrize(
    ("intent", "expected_status"),
    [("confirm", EXECUTING), ("cancel", CANCELLED)],
)
def test_natural_confirmation_reply_starts_existing_resolution_flow(intent, expected_status):
    engine = _engine()
    with Session(engine) as session:
        _event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        reply = _seed_confirmation_reply_notification(
            session,
            prepared.confirmation_id,
            message=f"natural reply: {intent}",
        )
        session.commit()

        result = process_async_chat_result_callback(
            session,
            _confirmation_reply_callback(
                reply,
                intent=intent,
                message=f"natural reply: {intent}",
            ),
        )

        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assert result["start_mutation_confirmation_worker"] is True
        assert result["confirmation_id"] == prepared.confirmation_id
        assert result["intent"] == intent
        assert row.status == expected_status
        assert session.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0


def test_unclear_natural_confirmation_reply_keeps_card_pending_and_asks_again():
    engine = _engine()
    with Session(engine) as session:
        _event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        reply = _seed_confirmation_reply_notification(
            session,
            prepared.confirmation_id,
            message="maybe",
        )
        session.commit()

        result = process_async_chat_result_callback(
            session,
            _confirmation_reply_callback(reply, intent="unclear", message="maybe"),
        )

        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assistant = session.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
        assert "start_mutation_confirmation_worker" not in result
        assert row.status == PENDING
        assert assistant.content == "supervisor response for unclear"


def test_new_request_supersedes_only_the_previous_pending_confirmation():
    engine = _engine()
    with Session(engine) as session:
        _event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        reply = _seed_confirmation_reply_notification(
            session,
            prepared.confirmation_id,
            message="What is my dose status?",
        )
        session.commit()

        result = process_async_chat_result_callback(
            session,
            _confirmation_reply_callback(
                reply,
                intent="new_request",
                message="What is my dose status?",
            ),
        )

        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assistant = session.query(ChatMessage).filter(ChatMessage.role == "assistant").one()
        assert result["status"] == "ok"
        assert row.status == SUPERSEDED
        assert assistant.content == "supervisor response for new_request"


def test_new_request_confirmation_classification_survives_async_continuation():
    response = AgentResponse(
        trace_id="trace-new-request-continuation",
        agent_name="multiturn_chat_agent",
        prompt_version_id="test",
        decision_type="async_continuation_requested",
        structured_payload={
            "mutation_confirmation_reply": {"intent": "new_request"},
            "async_continuation_required": True,
            "async_continuation_type": "side_effect_lookup",
            "tool_calls": [],
        },
        human_summary="continuing",
    )
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="I have a new symptom question.",
        current_time=datetime(2026, 4, 20, 9, 30),
        context={"pending_mutation_confirmation": {"display": {"question": "Apply?"}}},
    )

    system_continuation = build_async_continuation_request(request, response)
    worker_continuation = _continuation_payload(request.model_dump(mode="json"), response)

    assert "pending_mutation_confirmation" not in system_continuation.context
    assert system_continuation.context["pending_mutation_confirmation_reply_resolved"] == "new_request"
    assert "pending_mutation_confirmation" not in worker_continuation["context"]
    assert worker_continuation["context"]["pending_mutation_confirmation_reply_resolved"] == "new_request"


def test_late_unclear_reply_does_not_reopen_executing_confirmation():
    engine = _engine()
    with Session(engine) as session:
        _event, request = _seed_request(session)
        prepared = prepare_mutation_confirmation(session, request)
        confirming_reply = _seed_confirmation_reply_notification(
            session,
            prepared.confirmation_id,
            message="yes",
        )
        session.commit()
        process_async_chat_result_callback(
            session,
            _confirmation_reply_callback(confirming_reply, intent="confirm", message="yes"),
        )

        late_reply = _seed_confirmation_reply_notification(
            session,
            prepared.confirmation_id,
            message="maybe",
        )
        session.commit()
        result = process_async_chat_result_callback(
            session,
            _confirmation_reply_callback(late_reply, intent="unclear", message="maybe"),
        )

        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assert row.status == EXECUTING
        assert "start_mutation_confirmation_worker" not in result
        assert session.query(ChatMessage).filter(ChatMessage.role == "assistant").count() == 0


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
def test_revise_reply_supersedes_previous_confirmation_and_keeps_replacement_pending():
    engine = _engine()
    with Session(engine) as session:
        original_request = _seed_preference_request(session)
        original_request.arguments = {
            "predicate": "avoids_by_preference",
            "object_label": "watermelon",
            "object_type": "food",
            "safety_level": "soft",
            "evidence_text": "Record that I cannot eat watermelon.",
        }
        original = prepare_mutation_confirmation(session, original_request)
        reply_notification = _seed_confirmation_reply_notification(
            session,
            original.confirmation_id,
            message="Apply it as a hard ingestion restriction.",
        )

        replacement_request = original_request.model_copy(deep=True)
        replacement_request.trace_id = "trace-preference-revision"
        replacement_request.tool_call_id = "tool-call-preference-revision"
        replacement_request.arguments = {
            "predicate": "cannot_consume",
            "object_label": "watermelon",
            "object_type": "food",
            "safety_level": "hard",
            "evidence_text": "Apply it as a hard ingestion restriction.",
        }
        replacement_request.request_context = {
            **replacement_request.request_context,
            "message": "Apply it as a hard ingestion restriction.",
            "callback_context": {
                "notification_id": reply_notification.id,
                "conversation_id": "conversation-preference",
            },
        }
        replacement = prepare_mutation_confirmation(session, replacement_request)
        session.commit()

        callback = _confirmation_reply_callback(
            reply_notification,
            intent="revise",
            message="Apply it as a hard ingestion restriction.",
        )
        callback.response.structured_payload.update(
            {
                "mutation_confirmation_required": True,
                "mutation_confirmation": {
                    "confirmation_required": True,
                    "confirmation_id": replacement.confirmation_id,
                    "action_type": "agent_tool",
                    "action_name": UPSERT_NUTRITION_PREFERENCE_FACT,
                    "tool_call_id": replacement.tool_call_id,
                    "action_fingerprint": replacement.action_fingerprint,
                    "status": replacement.status,
                    "display": replacement.display,
                },
            }
        )

        result = process_async_chat_result_callback(session, callback)

        original_row = session.scalar(
            select(MutationConfirmation).where(MutationConfirmation.public_id == original.confirmation_id)
        )
        replacement_row = session.scalar(
            select(MutationConfirmation).where(MutationConfirmation.public_id == replacement.confirmation_id)
        )
        assert result["status"] == "ok"
        assert original_row.status == SUPERSEDED
        assert replacement_row.status == PENDING
        assert replacement_row.chat_message_id is not None
        assert session.query(NutritionPatientPreferenceTriple).count() == 0
def test_cannot_consume_confirmation_applies_as_a_hard_constraint():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_preference_request(session)
        request.arguments = {
            "predicate": "cannot_consume",
            "object_label": "watermelon",
            "object_type": "food",
            "evidence_text": "I cannot consume watermelon.",
        }
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()

        assert prepared.confirmation_required is True
        assert prepared.display["title"] == "\uc601\uc591 \uc81c\uc57d \uc815\ubcf4 \uae30\ub85d"
        assert prepared.display["details"][0]["value"] == "\uc12d\ucde8 \ubd88\uac00"

        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()
        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPSERT_NUTRITION_PREFERENCE_FACT,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="trace-cannot-consume-confirmed",
                source_event_type="nutrition_management_agent",
            ),
        )

        fact = session.query(NutritionPatientPreferenceTriple).one()
        assert result.status == APPLIED
        assert fact.predicate == "cannot_consume"
        assert fact.safety_level == "hard"


def test_nutrition_crud_actions_require_confirmation_and_execute_sequentially():
    engine = _engine()
    with Session(engine) as session:
        assert {
            CREATE_NUTRITION_MEAL_RECORD,
            UPDATE_NUTRITION_MEAL_RECORD,
            DELETE_NUTRITION_MEAL_RECORD,
            UPDATE_NUTRITION_FOOD_RECORD,
            DELETE_NUTRITION_FOOD_RECORD,
        }.issubset(ConfirmationActionRegistry.enabled_action_names())

        create_request = _seed_nutrition_crud_request(
            session,
            CREATE_NUTRITION_MEAL_RECORD,
            {
                "patient_id": "untrusted-patient",
                "meal_type": "lunch",
                "meal_date": "2026-04-20",
                "meal_time": "12:30:00",
                "foods": [
                    {
                        "food_ref_id": "food-rice",
                        "food_name": "rice",
                        "portion": "150g",
                        "nutrients": {
                            "\uce7c\ub85c\ub9ac": {"value": 220, "unit": "kcal"},
                            "\ub2e8\ubc31\uc9c8": {"value": 5, "unit": "g"},
                            "\ub098\ud2b8\ub968": {"value": 5, "unit": "mg"},
                        },
                    },
                    {
                        "food_ref_id": "food-soup",
                        "food_name": "soup",
                        "portion": "1 bowl",
                        "nutrients": {
                            "\uce7c\ub85c\ub9ac": {"value": 100, "unit": "kcal"},
                            "\ub2e8\ubc31\uc9c8": {"value": 7, "unit": "g"},
                            "\ub098\ud2b8\ub968": {"value": 300, "unit": "mg"},
                        },
                    },
                ],
            },
            trace_suffix="create-meal",
        )
        prepared_create = prepare_mutation_confirmation(session, create_request)
        session.commit()
        assert prepared_create.confirmation_required is True
        assert prepared_create.display["action_label"] == "\uae30\ub85d"
        assert session.query(NutritionMeal).count() == 0

        created = _confirm_and_execute_nutrition_mutation(
            session,
            prepared_create,
            CREATE_NUTRITION_MEAL_RECORD,
        )
        assert created.status == APPLIED
        meal = session.query(NutritionMeal).one()
        foods = session.scalars(
            select(NutritionFood)
            .where(NutritionFood.meal_id == meal.id)
            .order_by(NutritionFood.id)
        ).all()
        assert meal.patient_id == "demo-patient"
        assert [food.food_name for food in foods] == ["rice", "soup"]

        update_food_request = _seed_nutrition_crud_request(
            session,
            UPDATE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": meal.id,
                "food_id": foods[0].id,
                "food_name": "brown rice",
                "portion": "180g",
            },
            trace_suffix="update-food",
        )
        prepared_update_food = prepare_mutation_confirmation(session, update_food_request)
        session.commit()
        assert session.get(NutritionFood, foods[0].id).food_name == "rice"
        updated_food = _confirm_and_execute_nutrition_mutation(
            session,
            prepared_update_food,
            UPDATE_NUTRITION_FOOD_RECORD,
        )
        assert updated_food.status == APPLIED
        assert session.get(NutritionFood, foods[0].id).food_name == "brown rice"

        update_meal_request = _seed_nutrition_crud_request(
            session,
            UPDATE_NUTRITION_MEAL_RECORD,
            {"meal_id": meal.id, "meal_type": "dinner"},
            trace_suffix="update-meal",
        )
        prepared_update_meal = prepare_mutation_confirmation(session, update_meal_request)
        session.commit()
        assert session.get(NutritionMeal, meal.id).meal_type == "lunch"
        updated_meal = _confirm_and_execute_nutrition_mutation(
            session,
            prepared_update_meal,
            UPDATE_NUTRITION_MEAL_RECORD,
        )
        assert updated_meal.status == APPLIED
        assert session.get(NutritionMeal, meal.id).meal_type == "dinner"

        delete_food_request = _seed_nutrition_crud_request(
            session,
            DELETE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": meal.id,
                "food_id": foods[1].id,
                "delete_empty_meal": True,
            },
            trace_suffix="delete-food",
        )
        prepared_delete_food = prepare_mutation_confirmation(session, delete_food_request)
        session.commit()
        assert session.get(NutritionFood, foods[1].id) is not None
        deleted_food = _confirm_and_execute_nutrition_mutation(
            session,
            prepared_delete_food,
            DELETE_NUTRITION_FOOD_RECORD,
        )
        assert deleted_food.status == APPLIED
        assert session.get(NutritionFood, foods[1].id) is None
        assert session.get(NutritionMeal, meal.id) is not None

        delete_meal_request = _seed_nutrition_crud_request(
            session,
            DELETE_NUTRITION_MEAL_RECORD,
            {"meal_id": meal.id},
            trace_suffix="delete-meal",
        )
        prepared_delete_meal = prepare_mutation_confirmation(session, delete_meal_request)
        session.commit()
        assert session.get(NutritionMeal, meal.id) is not None
        deleted_meal = _confirm_and_execute_nutrition_mutation(
            session,
            prepared_delete_meal,
            DELETE_NUTRITION_MEAL_RECORD,
        )
        assert deleted_meal.status == APPLIED
        assert session.get(NutritionMeal, meal.id) is None


def test_nutrition_food_update_becomes_stale_when_target_changes():
    engine = _engine()
    with Session(engine) as session:
        meal = NutritionMeal(
            patient_id="demo-patient",
            meal_type="lunch",
            meal_date=date(2026, 4, 20),
            meal_time="12:00:00",
        )
        session.add(meal)
        session.flush()
        food = NutritionFood(
            meal_id=meal.id,
            food_ref_id="food-original",
            food_name="original",
            portion="100g",
        )
        session.add(food)
        session.commit()

        request = _seed_nutrition_crud_request(
            session,
            UPDATE_NUTRITION_FOOD_RECORD,
            {
                "meal_id": meal.id,
                "food_id": food.id,
                "food_name": "requested",
            },
            trace_suffix="stale-food",
        )
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        food.food_name = "changed elsewhere"
        session.commit()

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=UPDATE_NUTRITION_FOOD_RECORD,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="confirmed-stale-food",
                source_event_type="nutrition_management_agent",
            ),
        )
        session.commit()

        assert result.status == STALE
        assert session.get(NutritionFood, food.id).food_name == "changed elsewhere"


def test_failed_nutrition_create_rolls_back_partial_domain_changes():
    engine = _engine()
    with Session(engine) as session:
        request = _seed_nutrition_crud_request(
            session,
            CREATE_NUTRITION_MEAL_RECORD,
            {
                "meal_type": "lunch",
                "meal_date": "2026-04-20",
                "foods": [
                    {
                        "food_ref_id": "valid-food",
                        "food_name": "valid food",
                        "portion": "100g",
                        "nutrients": {},
                    },
                    {
                        "food_ref_id": "invalid-food",
                        "food_name": "   ",
                        "portion": "100g",
                        "nutrients": {},
                    },
                ],
            },
            trace_suffix="failed-create",
        )
        prepared = prepare_mutation_confirmation(session, request)
        session.commit()
        begin_mutation_resolution(
            session,
            prepared.confirmation_id,
            "confirm",
            patient_id="demo-patient",
        )
        session.commit()

        result = execute_confirmed_mutation(
            session,
            prepared.confirmation_id,
            ConfirmedMutationExecutionRequest(
                confirmation_id=prepared.confirmation_id,
                action_name=CREATE_NUTRITION_MEAL_RECORD,
                action_fingerprint=prepared.action_fingerprint,
                trace_id="confirmed-failed-create",
                source_event_type="nutrition_management_agent",
            ),
        )
        session.commit()

        assert result.status == FAILED
        assert session.query(NutritionMeal).count() == 0
        assert session.query(NutritionFood).count() == 0
        assert session.query(DailyNutritionCheck).count() == 0
        row = session.scalar(
            select(MutationConfirmation).where(
                MutationConfirmation.public_id == prepared.confirmation_id
            )
        )
        assert row.status == FAILED
