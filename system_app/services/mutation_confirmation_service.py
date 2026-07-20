from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

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
from shared.json_utils import dump_json, parse_json_object
from shared.schemas import (
    ConfirmedMutationExecutionRequest,
    ConfirmedMutationExecutionResult,
    DoseTakenToolRequest,
    DoseTakenToolResult,
    MutationConfirmationPrepareRequest,
    MutationConfirmationPrepareResult,
    NutritionFoodDeleteRequest,
    NutritionFoodDeleteResult,
    NutritionFoodUpdateRequest,
    NutritionFoodUpdateResult,
    NutritionMealDeleteRequest,
    NutritionMealDeleteResult,
    NutritionMealRecordRequest,
    NutritionMealRecordResult,
    NutritionMealUpdateRequest,
    NutritionMealUpdateResult,
    NutritionPreferenceFactRequest,
    NutritionPreferenceFactResult,
)
from shared.time_utils import utc_now
from system_app.models import (
    ChatMessage,
    DoseEvent,
    MutationConfirmation,
    Notification,
    NutritionFood,
    NutritionMeal,
    NutritionOntologyNode,
    NutritionPatientPreferenceTriple,
)
from system_app.services.agent_callback_service import apply_agent_dose_taken_request
from system_app.services.clock_service import ensure_clock
from system_app.services.nutrition_preference_service import (
    HARD_CONSTRAINT_PREDICATES,
    normalize_ontology_label,
    nutrition_preference_summary,
    ontology_node_key,
    preference_fact_view,
    record_preference_fact,
)
from system_app.services.nutrition_service import (
    MEAL_TYPE_LABELS,
    delete_food,
    delete_meal,
    food_view,
    meal_view,
    meals_for_date,
    record_meal,
    update_food,
    update_meal,
)

PENDING = "pending"
EXECUTING = "executing"
APPLIED = "applied"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"
STALE = "stale"
FAILED = "failed"
EXECUTION_LEASE = timedelta(minutes=2)
NUTRITION_CRUD_ACTIONS = frozenset(
    {
        CREATE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
    }
)


def prepare_mutation_confirmation(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
) -> MutationConfirmationPrepareResult:
    action = ConfirmationActionRegistry.get(payload.action_name)
    if action is None or not ConfirmationActionRegistry.requires_confirmation(payload.action_name):
        raise ValueError("mutation_confirmation_action_not_enabled")
    if payload.action_name in NUTRITION_CRUD_ACTIONS:
        return _prepare_nutrition_crud_confirmation(
            session,
            payload,
            action_type=action.action_type,
        )
    if payload.action_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        return _prepare_nutrition_preference_confirmation(
            session,
            payload,
            action_type=action.action_type,
        )
    if payload.action_name != UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        raise ValueError("mutation_confirmation_action_not_implemented")

    arguments, event = _prepare_dose_arguments(session, payload)
    snapshot = _dose_snapshot(event)
    snapshot_hash = _hash_payload(snapshot)
    fingerprint = _hash_payload(
        {
            "patient_id": payload.patient_id,
            "action_name": payload.action_name,
            "arguments": _dose_fingerprint_arguments(arguments),
        }
    )
    resolution = payload.request_context.get("mutation_resolution")
    if isinstance(resolution, dict) and resolution.get("action_fingerprint") == fingerprint:
        status = str(resolution.get("status") or "")
        return MutationConfirmationPrepareResult(
            confirmation_required=False,
            action_name=payload.action_name,
            tool_call_id=payload.tool_call_id,
            action_fingerprint=fingerprint,
            status="already_applied" if status == APPLIED else status or "skipped",
            execution_result=resolution.get("tool_result") if isinstance(resolution.get("tool_result"), dict) else {},
        )
    if event.status == "taken":
        result = DoseTakenToolResult(
            dose_event_id=event.id,
            status="taken",
            taken_at=event.taken_at,
            message=f"{event.slot_label} {event.medication_name} \ubcf5\uc57d\uc740 \uc774\ubbf8 \uc644\ub8cc\ub85c \uae30\ub85d\ub418\uc5b4 \uc788\uc2b5\ub2c8\ub2e4.",
        )
        return MutationConfirmationPrepareResult(
            confirmation_required=False,
            action_name=payload.action_name,
            tool_call_id=payload.tool_call_id,
            action_fingerprint=fingerprint,
            status="already_applied",
            execution_result=result.model_dump(mode="json"),
        )

    notification_id, conversation_id, origin_chat_id = _origin_context(session, payload.request_context)
    idempotency_key = f"{payload.trace_id}:{payload.action_name}:{fingerprint}:{snapshot_hash}"
    existing = session.scalar(
        select(MutationConfirmation).where(MutationConfirmation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return _prepare_result(existing)

    pending_duplicate = session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.patient_id == payload.patient_id,
            MutationConfirmation.action_name == payload.action_name,
            MutationConfirmation.action_fingerprint == fingerprint,
            MutationConfirmation.target_snapshot_hash == snapshot_hash,
            MutationConfirmation.status == PENDING,
        )
        .order_by(MutationConfirmation.id.desc())
    )
    if pending_duplicate is not None:
        return _prepare_result(pending_duplicate)

    _supersede_pending(session, payload.patient_id)
    status = SUPERSEDED if _has_newer_general_message(session, payload.patient_id, origin_chat_id) else PENDING
    display = {
        "title": "\ubcf5\uc57d \uc644\ub8cc \uae30\ub85d",
        "question": f"{event.slot_label} {event.medication_name} \ubcf5\uc57d\uc744 \uc644\ub8cc\ub85c \uae30\ub85d\ud560\uae4c\uc694?",
        "action_label": "\uae30\ub85d",
        "target": event.medication_name,
        "details": [
            {"label": "\ubcf5\uc57d \uc2dc\uac04", "value": event.slot_label},
            {"label": "\uc608\uc815 \uc2dc\uac01", "value": event.scheduled_for.isoformat(timespec="minutes")},
            {"label": "\ubcc0\uacbd \uc804", "value": _dose_status_label(event.status)},
            {"label": "\ubcc0\uacbd \ud6c4", "value": "\ubcf5\uc57d \uc644\ub8cc"},
        ],
    }
    row = MutationConfirmation(
        public_id=uuid4().hex,
        patient_id=payload.patient_id,
        origin_request_notification_id=notification_id,
        conversation_id=conversation_id,
        origin_trace_id=payload.trace_id,
        origin_agent=payload.source_event_type,
        source_event_type=payload.source_event_type,
        action_type=action.action_type,
        action_name=payload.action_name,
        tool_call_id=payload.tool_call_id,
        arguments_json=dump_json(arguments),
        action_fingerprint=fingerprint,
        target_snapshot_json=dump_json(snapshot),
        target_snapshot_hash=snapshot_hash,
        display_json=dump_json(display),
        continuation_json=dump_json(_continuation_context(payload.request_context)),
        idempotency_key=idempotency_key,
        status=status,
        resolved_at=utc_now() if status == SUPERSEDED else None,
    )
    session.add(row)
    session.flush()
    return _prepare_result(row)


def execute_confirmed_mutation(
    session: Session,
    public_id: str,
    payload: ConfirmedMutationExecutionRequest,
) -> ConfirmedMutationExecutionResult:
    row = session.scalar(
        select(MutationConfirmation)
        .where(MutationConfirmation.public_id == public_id)
        .with_for_update()
    )
    if row is None:
        raise ValueError("mutation_confirmation_not_found")
    if row.status == APPLIED:
        return _execution_result(row)
    if row.status != EXECUTING:
        raise ValueError(f"mutation_confirmation_not_executing:{row.status}")
    if payload.confirmation_id != row.public_id or payload.action_name != row.action_name:
        raise ValueError("mutation_confirmation_action_mismatch")
    if payload.action_fingerprint != row.action_fingerprint:
        raise ValueError("mutation_confirmation_fingerprint_mismatch")
    if row.action_name in NUTRITION_CRUD_ACTIONS:
        return _execute_nutrition_crud_mutation(session, row)
    if row.action_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        return _execute_nutrition_preference_mutation(session, row)

    if row.action_name != UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        row.status = FAILED
        row.error_message = "mutation_confirmation_action_not_implemented"
        row.resolved_at = utc_now()
        session.flush()
        return _execution_result(row)

    arguments = parse_json_object(row.arguments_json)
    request = DoseTakenToolRequest.model_validate(arguments)
    event = session.get(DoseEvent, request.dose_event_id)
    current_snapshot = _dose_snapshot(event) if event is not None else {}
    if _hash_payload(current_snapshot) != row.target_snapshot_hash:
        row.status = STALE
        row.error_message = "mutation_confirmation_snapshot_changed"
        row.resolved_at = utc_now()
        session.flush()
        return _execution_result(row)

    result = apply_agent_dose_taken_request(session, request)
    if result.status != "taken":
        row.status = FAILED
        row.error_message = result.message
    else:
        row.status = APPLIED
        row.result_json = dump_json(result.model_dump(mode="json"))
        row.error_message = ""
    row.resolved_at = utc_now()
    session.flush()
    return _execution_result(row)


def begin_mutation_resolution(
    session: Session,
    public_id: str,
    resolution: str,
    *,
    patient_id: str,
) -> MutationConfirmation:
    row = session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.public_id == public_id,
            MutationConfirmation.patient_id == patient_id,
        )
        .with_for_update()
    )
    if row is None:
        raise ValueError("mutation_confirmation_not_found")
    if row.status in {APPLIED, CANCELLED}:
        return row
    if row.status != PENDING:
        raise ValueError(f"mutation_confirmation_not_pending:{row.status}")
    if resolution == "confirm":
        row.status = EXECUTING
        row.execution_started_at = utc_now()
        row.error_message = ""
    elif resolution == "cancel":
        row.status = CANCELLED
        row.resolved_at = utc_now()
    else:
        raise ValueError("mutation_confirmation_invalid_resolution")
    session.flush()
    mirror_confirmation_card_status(session, row)
    return row


def supersede_pending_confirmations(session: Session, patient_id: str) -> int:
    return _supersede_pending(session, patient_id)


def pending_confirmation_for_patient(session: Session, patient_id: str) -> MutationConfirmation | None:
    return session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.patient_id == patient_id,
            MutationConfirmation.status == PENDING,
        )
        .order_by(MutationConfirmation.created_at.desc(), MutationConfirmation.id.desc())
        .limit(1)
    )


def pending_confirmation_reply_context(
    session: Session,
    row: MutationConfirmation,
) -> dict[str, object]:
    origin = (
        session.get(Notification, row.origin_request_notification_id)
        if row.origin_request_notification_id is not None
        else None
    )
    origin_metadata = parse_json_object(origin.metadata_json) if origin is not None else {}
    return {
        "display": parse_json_object(row.display_json),
        "original_request": str(origin_metadata.get("request_message") or ""),
    }


def supersede_mutation_confirmation(
    session: Session,
    public_id: str,
    *,
    patient_id: str,
) -> bool:
    row = session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.public_id == public_id,
            MutationConfirmation.patient_id == patient_id,
        )
        .with_for_update()
    )
    if row is None or row.status != PENDING:
        return False
    row.status = SUPERSEDED
    row.resolved_at = utc_now()
    mirror_confirmation_card_status(session, row)
    session.flush()
    return True


def has_executing_confirmation(session: Session, patient_id: str) -> bool:
    return (
        session.scalar(
            select(MutationConfirmation.id)
            .where(
                MutationConfirmation.patient_id == patient_id,
                MutationConfirmation.status == EXECUTING,
            )
            .limit(1)
        )
        is not None
    )


def recover_expired_confirmations(session: Session, patient_id: str | None = None) -> int:
    cutoff = utc_now() - EXECUTION_LEASE
    stmt = select(MutationConfirmation).where(
        MutationConfirmation.status == EXECUTING,
        MutationConfirmation.execution_started_at < cutoff,
    )
    if patient_id:
        stmt = stmt.where(MutationConfirmation.patient_id == patient_id)
    rows = list(session.scalars(stmt).all())
    for row in rows:
        row.status = FAILED
        row.error_message = "mutation_confirmation_execution_lease_expired"
        row.resolved_at = utc_now()
        mirror_confirmation_card_status(session, row)
    if rows:
        session.flush()
    return len(rows)


def confirmation_for_response(
    session: Session,
    response_payload: dict,
) -> MutationConfirmation | None:
    proposal = response_payload.get("mutation_confirmation")
    if not isinstance(proposal, dict):
        return None
    public_id = str(proposal.get("confirmation_id") or "")
    if not public_id:
        return None
    return session.scalar(
        select(MutationConfirmation).where(MutationConfirmation.public_id == public_id)
    )


def attach_confirmation_chat_message(
    session: Session,
    row: MutationConfirmation,
    chat_message: ChatMessage,
) -> None:
    row.chat_message_id = chat_message.id
    mirror_confirmation_card_status(session, row)
    session.flush()


def mirror_confirmation_card_status(session: Session, row: MutationConfirmation) -> None:
    if row.chat_message_id is None:
        return
    message = session.get(ChatMessage, row.chat_message_id)
    if message is None:
        return
    metadata = parse_json_object(message.metadata_json)
    card = metadata.get("mutation_confirmation")
    if not isinstance(card, dict):
        card = confirmation_card_payload(row)
    card["status"] = row.status
    card["error"] = row.error_message
    metadata["mutation_confirmation"] = card
    message.metadata_json = dump_json(metadata)
    session.flush()


def confirmation_card_payload(row: MutationConfirmation) -> dict:
    return {
        "confirmation_id": row.public_id,
        "status": row.status,
        "action_type": row.action_type,
        "action_name": row.action_name,
        "display": parse_json_object(row.display_json),
        "error": row.error_message,
    }


def _prepare_nutrition_crud_confirmation(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
    *,
    action_type: str,
) -> MutationConfirmationPrepareResult:
    arguments = _prepare_nutrition_crud_arguments(session, payload)
    snapshot = _nutrition_crud_snapshot(
        session,
        payload.action_name,
        arguments,
        require_target=True,
    )
    snapshot_hash = _hash_payload(snapshot)
    fingerprint = _hash_payload(
        {
            "patient_id": payload.patient_id,
            "action_name": payload.action_name,
            "arguments": arguments,
        }
    )

    resolution = payload.request_context.get("mutation_resolution")
    if isinstance(resolution, dict) and resolution.get("action_fingerprint") == fingerprint:
        status = str(resolution.get("status") or "")
        return MutationConfirmationPrepareResult(
            confirmation_required=False,
            action_name=payload.action_name,
            tool_call_id=payload.tool_call_id,
            action_fingerprint=fingerprint,
            status="already_applied" if status == APPLIED else status or "skipped",
            execution_result=resolution.get("tool_result") if isinstance(resolution.get("tool_result"), dict) else {},
        )

    notification_id, conversation_id, origin_chat_id = _origin_context(session, payload.request_context)
    idempotency_key = f"{payload.trace_id}:{payload.action_name}:{fingerprint}:{snapshot_hash}"
    existing = session.scalar(
        select(MutationConfirmation).where(MutationConfirmation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return _prepare_result(existing)

    pending_duplicate = session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.patient_id == payload.patient_id,
            MutationConfirmation.action_name == payload.action_name,
            MutationConfirmation.action_fingerprint == fingerprint,
            MutationConfirmation.target_snapshot_hash == snapshot_hash,
            MutationConfirmation.status == PENDING,
        )
        .order_by(MutationConfirmation.id.desc())
    )
    if pending_duplicate is not None:
        return _prepare_result(pending_duplicate)

    _supersede_pending(session, payload.patient_id)
    status = SUPERSEDED if _has_newer_general_message(session, payload.patient_id, origin_chat_id) else PENDING
    row = MutationConfirmation(
        public_id=uuid4().hex,
        patient_id=payload.patient_id,
        origin_request_notification_id=notification_id,
        conversation_id=conversation_id,
        origin_trace_id=payload.trace_id,
        origin_agent=payload.source_event_type,
        source_event_type=payload.source_event_type,
        action_type=action_type,
        action_name=payload.action_name,
        tool_call_id=payload.tool_call_id,
        arguments_json=dump_json(arguments),
        action_fingerprint=fingerprint,
        target_snapshot_json=dump_json(snapshot),
        target_snapshot_hash=snapshot_hash,
        display_json=dump_json(
            _nutrition_crud_display(payload.action_name, arguments, snapshot)
        ),
        continuation_json=dump_json(_continuation_context(payload.request_context)),
        idempotency_key=idempotency_key,
        status=status,
        resolved_at=utc_now() if status == SUPERSEDED else None,
    )
    session.add(row)
    session.flush()
    return _prepare_result(row)


def _execute_nutrition_crud_mutation(
    session: Session,
    row: MutationConfirmation,
) -> ConfirmedMutationExecutionResult:
    arguments = parse_json_object(row.arguments_json)
    current_snapshot = _nutrition_crud_snapshot(
        session,
        row.action_name,
        arguments,
        require_target=False,
    )
    if _hash_payload(current_snapshot) != row.target_snapshot_hash:
        row.status = STALE
        row.error_message = "mutation_confirmation_snapshot_changed"
        row.resolved_at = utc_now()
        session.flush()
        return _execution_result(row)

    try:
        with session.begin_nested():
            result = _run_nutrition_crud_mutation(session, row.action_name, arguments)
    except ValueError as exc:
        row.status = FAILED
        row.error_message = str(exc)
        row.resolved_at = utc_now()
        session.flush()
        return _execution_result(row)

    row.status = APPLIED
    row.result_json = dump_json(result)
    row.error_message = ""
    row.resolved_at = utc_now()
    session.flush()
    return _execution_result(row)


def _run_nutrition_crud_mutation(
    session: Session,
    action_name: str,
    arguments: dict,
) -> dict:
    if action_name == CREATE_NUTRITION_MEAL_RECORD:
        request = NutritionMealRecordRequest.model_validate(arguments)
        result = record_meal(
            session,
            patient_id=request.patient_id,
            foods=[food.model_dump(mode="json") for food in request.foods],
            meal_type=request.meal_type,
            meal_date=request.meal_date,
            meal_time=request.meal_time,
            scenario_key=request.scenario_key,
            description=request.description,
        )
        NutritionMealRecordResult.model_validate(result)
        return result

    if action_name == UPDATE_NUTRITION_MEAL_RECORD:
        meal_id = _positive_int(arguments, "meal_id")
        request = NutritionMealUpdateRequest.model_validate(
            {key: value for key, value in arguments.items() if key != "meal_id"}
        )
        result = update_meal(
            session,
            meal_id=meal_id,
            patient_id=request.patient_id,
            foods=[food.model_dump(mode="json") for food in request.foods]
            if request.foods is not None
            else None,
            meal_type=request.meal_type,
            meal_date=request.meal_date,
            meal_time=request.meal_time,
            scenario_key=request.scenario_key,
            description=request.description,
            reason=request.reason,
        )
        NutritionMealUpdateResult.model_validate(result)
        return result

    if action_name == DELETE_NUTRITION_MEAL_RECORD:
        meal_id = _positive_int(arguments, "meal_id")
        request = NutritionMealDeleteRequest.model_validate(
            {key: value for key, value in arguments.items() if key != "meal_id"}
        )
        result = delete_meal(
            session,
            meal_id=meal_id,
            patient_id=request.patient_id,
            reason=request.reason,
        )
        NutritionMealDeleteResult.model_validate(result)
        return result

    if action_name == UPDATE_NUTRITION_FOOD_RECORD:
        meal_id = _positive_int(arguments, "meal_id")
        food_id = _positive_int(arguments, "food_id")
        request = NutritionFoodUpdateRequest.model_validate(
            {
                key: value
                for key, value in arguments.items()
                if key not in {"meal_id", "food_id"}
            }
        )
        result = update_food(
            session,
            meal_id=meal_id,
            food_id=food_id,
            patient_id=request.patient_id,
            food_ref_id=request.food_ref_id,
            food_name=request.food_name,
            portion=request.portion,
            nutrients=request.nutrients,
            reason=request.reason,
        )
        NutritionFoodUpdateResult.model_validate(result)
        return result

    if action_name == DELETE_NUTRITION_FOOD_RECORD:
        meal_id = _positive_int(arguments, "meal_id")
        food_id = _positive_int(arguments, "food_id")
        request = NutritionFoodDeleteRequest.model_validate(
            {
                key: value
                for key, value in arguments.items()
                if key not in {"meal_id", "food_id"}
            }
        )
        result = delete_food(
            session,
            meal_id=meal_id,
            food_id=food_id,
            patient_id=request.patient_id,
            reason=request.reason,
            delete_empty_meal=request.delete_empty_meal,
        )
        NutritionFoodDeleteResult.model_validate(result)
        return result

    raise ValueError("mutation_confirmation_action_not_implemented")


def _prepare_nutrition_crud_arguments(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
) -> dict:
    raw = {**payload.arguments, "patient_id": payload.patient_id}
    if payload.action_name == CREATE_NUTRITION_MEAL_RECORD:
        request = NutritionMealRecordRequest.model_validate(raw)
        if not request.foods:
            raise ValueError("foods_required")
        clock = ensure_clock(session)
        if request.meal_date is None:
            request.meal_date = clock.current_time.date()
        if request.meal_time is None:
            request.meal_time = clock.current_time.strftime("%H:%M:%S")
        return request.model_dump(mode="json")

    if payload.action_name == UPDATE_NUTRITION_MEAL_RECORD:
        meal_id = _positive_int(raw, "meal_id")
        request = NutritionMealUpdateRequest.model_validate(
            {key: value for key, value in raw.items() if key != "meal_id"}
        )
        if request.foods is not None and not request.foods:
            raise ValueError("foods_required")
        if all(
            value is None
            for value in (
                request.foods,
                request.meal_type,
                request.meal_date,
                request.meal_time,
                request.scenario_key,
                request.description,
            )
        ):
            raise ValueError("nutrition_meal_update_empty")
        return {
            "meal_id": meal_id,
            **request.model_dump(mode="json", exclude_none=True),
        }

    if payload.action_name == DELETE_NUTRITION_MEAL_RECORD:
        meal_id = _positive_int(raw, "meal_id")
        request = NutritionMealDeleteRequest.model_validate(
            {key: value for key, value in raw.items() if key != "meal_id"}
        )
        return {"meal_id": meal_id, **request.model_dump(mode="json")}

    if payload.action_name == UPDATE_NUTRITION_FOOD_RECORD:
        meal_id = _positive_int(raw, "meal_id")
        food_id = _positive_int(raw, "food_id")
        request = NutritionFoodUpdateRequest.model_validate(
            {
                key: value
                for key, value in raw.items()
                if key not in {"meal_id", "food_id"}
            }
        )
        if all(
            value is None
            for value in (
                request.food_ref_id,
                request.food_name,
                request.portion,
                request.nutrients,
            )
        ):
            raise ValueError("nutrition_food_update_empty")
        return {
            "meal_id": meal_id,
            "food_id": food_id,
            **request.model_dump(mode="json", exclude_none=True),
        }

    if payload.action_name == DELETE_NUTRITION_FOOD_RECORD:
        meal_id = _positive_int(raw, "meal_id")
        food_id = _positive_int(raw, "food_id")
        request = NutritionFoodDeleteRequest.model_validate(
            {
                key: value
                for key, value in raw.items()
                if key not in {"meal_id", "food_id"}
            }
        )
        return {
            "meal_id": meal_id,
            "food_id": food_id,
            **request.model_dump(mode="json"),
        }

    raise ValueError("mutation_confirmation_action_not_implemented")


def _nutrition_crud_snapshot(
    session: Session,
    action_name: str,
    arguments: dict,
    *,
    require_target: bool,
) -> dict:
    patient_id = str(arguments.get("patient_id") or "")
    if action_name == CREATE_NUTRITION_MEAL_RECORD:
        target_date = date.fromisoformat(str(arguments.get("meal_date") or ""))
        return {
            "patient_id": patient_id,
            "meal_date": target_date.isoformat(),
            "meals": [
                meal_view(session, meal)
                for meal in meals_for_date(session, patient_id, target_date)
            ],
        }

    meal_id = _positive_int(arguments, "meal_id")
    meal = session.scalar(
        select(NutritionMeal).where(
            NutritionMeal.id == meal_id,
            NutritionMeal.patient_id == patient_id,
        )
    )
    if meal is None:
        if require_target:
            raise ValueError("nutrition_meal_not_found")
        return {}

    if action_name in {
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
    }:
        return {"meal": meal_view(session, meal)}

    food_id = _positive_int(arguments, "food_id")
    food = session.scalar(
        select(NutritionFood).where(
            NutritionFood.id == food_id,
            NutritionFood.meal_id == meal.id,
        )
    )
    if food is None:
        if require_target:
            raise ValueError("nutrition_food_not_found")
        return {}
    return {
        "meal": {
            "id": meal.id,
            "patient_id": meal.patient_id,
            "meal_type": meal.meal_type,
            "meal_date": meal.meal_date.isoformat(),
            "meal_time": meal.meal_time,
        },
        "food": food_view(food),
    }


def _nutrition_crud_display(
    action_name: str,
    arguments: dict,
    snapshot: dict,
) -> dict:
    if action_name == CREATE_NUTRITION_MEAL_RECORD:
        meal_label = MEAL_TYPE_LABELS.get(
            str(arguments.get("meal_type") or ""),
            str(arguments.get("meal_type") or ""),
        )
        food_names = _food_payload_names(arguments.get("foods"))
        return {
            "title": "\uc2dd\uc0ac \uae30\ub85d",
            "question": f"{arguments.get('meal_date')} {meal_label} \uc2dd\uc0ac\ub85c {food_names}\uc744(\ub97c) \uae30\ub85d\ud560\uae4c\uc694?",
            "action_label": "\uae30\ub85d",
            "target": food_names,
            "details": [
                {"label": "\ub0a0\uc9dc", "value": str(arguments.get("meal_date") or "")},
                {"label": "\uc2dd\uc0ac \uc885\ub958", "value": meal_label},
                {"label": "\uc74c\uc2dd", "value": food_names},
                {"label": "\uc12d\ucde8\ub7c9", "value": _food_payload_portions(arguments.get("foods"))},
            ],
        }

    meal = snapshot.get("meal") if isinstance(snapshot.get("meal"), dict) else {}
    meal_label = str(meal.get("meal_label") or MEAL_TYPE_LABELS.get(str(meal.get("meal_type") or ""), ""))
    meal_date = str(meal.get("meal_date") or "")
    if action_name == UPDATE_NUTRITION_MEAL_RECORD:
        before_foods = _food_payload_names(meal.get("foods"))
        after_foods = (
            _food_payload_names(arguments.get("foods"))
            if "foods" in arguments
            else before_foods
        )
        after_label = MEAL_TYPE_LABELS.get(
            str(arguments.get("meal_type") or meal.get("meal_type") or ""),
            str(arguments.get("meal_type") or meal.get("meal_type") or ""),
        )
        before_time = str(meal.get("meal_time") or "")
        after_date = str(arguments.get("meal_date") or meal_date)
        after_time = str(arguments.get("meal_time") or before_time)
        details = [
            {
                "label": "\ubcc0\uacbd \uc804",
                "value": f"{meal_date} {before_time} {meal_label}: {before_foods}".strip(),
            },
            {
                "label": "\ubcc0\uacbd \ud6c4",
                "value": f"{after_date} {after_time} {after_label}: {after_foods}".strip(),
            },
        ]
        if "description" in arguments:
            details.append(
                {
                    "label": "\uc124\uba85",
                    "value": str(arguments.get("description") or "\uc5c6\uc74c"),
                }
            )
        return {
            "title": "\uc2dd\uc0ac \uae30\ub85d \uc218\uc815",
            "question": f"{meal_date} {meal_label} \uc2dd\uc0ac \uae30\ub85d\uc744 \uc218\uc815\ud560\uae4c\uc694?",
            "action_label": "\uc218\uc815",
            "target": before_foods or meal_label,
            "details": details,
        }

    if action_name == DELETE_NUTRITION_MEAL_RECORD:
        food_names = _food_payload_names(meal.get("foods"))
        return {
            "title": "\uc2dd\uc0ac \uae30\ub85d \uc0ad\uc81c",
            "question": f"{meal_date} {meal_label} \uc2dd\uc0ac \uae30\ub85d\uc744 \uc0ad\uc81c\ud560\uae4c\uc694?",
            "action_label": "\uc0ad\uc81c",
            "target": food_names or meal_label,
            "details": [
                {"label": "\ub0a0\uc9dc", "value": meal_date},
                {"label": "\uc2dd\uc0ac \uc885\ub958", "value": meal_label},
                {"label": "\uc74c\uc2dd", "value": food_names},
            ],
        }

    food = snapshot.get("food") if isinstance(snapshot.get("food"), dict) else {}
    food_name = str(food.get("food_name") or "")
    if action_name == UPDATE_NUTRITION_FOOD_RECORD:
        next_name = str(arguments.get("food_name") or food_name)
        before_portion = str(food.get("portion") or "")
        next_portion = str(arguments.get("portion") or before_portion)
        details = [
            {"label": "\ubcc0\uacbd \uc804", "value": f"{food_name} {before_portion}".strip()},
            {"label": "\ubcc0\uacbd \ud6c4", "value": f"{next_name} {next_portion}".strip()},
        ]
        if "nutrients" in arguments:
            details.append({"label": "\uc601\uc591\uc131\ubd84", "value": "\uc120\ud0dd\ud55c \uae30\uc900\ub7c9\uc73c\ub85c \uc7ac\uacc4\uc0b0"})
        if next_name != food_name:
            question = f"{food_name} \uae30\ub85d\uc744 {next_name}(\uc73c)\ub85c \uc218\uc815\ud560\uae4c\uc694?"
        elif next_portion != before_portion:
            question = f"{food_name} \uc12d\ucde8\ub7c9\uc744 {next_portion}(\uc73c)\ub85c \uc218\uc815\ud560\uae4c\uc694?"
        else:
            question = f"{food_name} \uc601\uc591\uc131\ubd84 \uae30\ub85d\uc744 \uc218\uc815\ud560\uae4c\uc694?"
        return {
            "title": "\uc74c\uc2dd \uae30\ub85d \uc218\uc815",
            "question": question,
            "action_label": "\uc218\uc815",
            "target": food_name,
            "details": details,
        }

    return {
        "title": "\uc74c\uc2dd \uae30\ub85d \uc0ad\uc81c",
        "question": f"{meal_date} {meal_label} \uc2dd\uc0ac\uc5d0\uc11c {food_name}\uc744(\ub97c) \uc0ad\uc81c\ud560\uae4c\uc694?",
        "action_label": "\uc0ad\uc81c",
        "target": food_name,
        "details": [
            {"label": "\ub0a0\uc9dc", "value": meal_date},
            {"label": "\uc2dd\uc0ac \uc885\ub958", "value": meal_label},
            {"label": "\uc74c\uc2dd", "value": food_name},
            {"label": "\uc12d\ucde8\ub7c9", "value": str(food.get("portion") or "")},
        ],
    }


def _food_payload_names(value: object) -> str:
    if not isinstance(value, list):
        return ""
    names = [
        str(item.get("food_name") or item.get("name") or "").strip()
        for item in value
        if isinstance(item, dict)
    ]
    return ", ".join(name for name in names if name)


def _food_payload_portions(value: object) -> str:
    if not isinstance(value, list):
        return ""
    portions = [
        f"{str(item.get('food_name') or item.get('name') or '').strip()} {str(item.get('portion') or '').strip()}".strip()
        for item in value
        if isinstance(item, dict)
    ]
    return ", ".join(portion for portion in portions if portion)


def _positive_int(arguments: dict, key: str) -> int:
    try:
        value = int(arguments.get(key))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key}_required") from exc
    if value <= 0:
        raise ValueError(f"{key}_required")
    return value


def _prepare_nutrition_preference_confirmation(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
    *,
    action_type: str,
) -> MutationConfirmationPrepareResult:
    arguments, request, node, fact = _prepare_nutrition_preference_arguments(session, payload)
    snapshot = _nutrition_preference_snapshot(node, fact)
    snapshot_hash = _hash_payload(snapshot)
    desired_state = _nutrition_preference_desired_state(request)
    fingerprint = _hash_payload(
        {
            "patient_id": payload.patient_id,
            "action_name": payload.action_name,
            "desired_state": desired_state,
        }
    )

    resolution = payload.request_context.get("mutation_resolution")
    if isinstance(resolution, dict) and resolution.get("action_fingerprint") == fingerprint:
        status = str(resolution.get("status") or "")
        return MutationConfirmationPrepareResult(
            confirmation_required=False,
            action_name=payload.action_name,
            tool_call_id=payload.tool_call_id,
            action_fingerprint=fingerprint,
            status="already_applied" if status == APPLIED else status or "skipped",
            execution_result=resolution.get("tool_result") if isinstance(resolution.get("tool_result"), dict) else {},
        )

    if _nutrition_preference_is_applied(fact, desired_state):
        result = NutritionPreferenceFactResult(
            success=True,
            fact=preference_fact_view(fact, node),
            preferences=nutrition_preference_summary(session, patient_id=payload.patient_id),
        )
        return MutationConfirmationPrepareResult(
            confirmation_required=False,
            action_name=payload.action_name,
            tool_call_id=payload.tool_call_id,
            action_fingerprint=fingerprint,
            status="already_applied",
            execution_result=result.model_dump(mode="json"),
        )

    notification_id, conversation_id, origin_chat_id = _origin_context(session, payload.request_context)
    idempotency_key = f"{payload.trace_id}:{payload.action_name}:{fingerprint}:{snapshot_hash}"
    existing = session.scalar(
        select(MutationConfirmation).where(MutationConfirmation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return _prepare_result(existing)

    pending_duplicate = session.scalar(
        select(MutationConfirmation)
        .where(
            MutationConfirmation.patient_id == payload.patient_id,
            MutationConfirmation.action_name == payload.action_name,
            MutationConfirmation.action_fingerprint == fingerprint,
            MutationConfirmation.target_snapshot_hash == snapshot_hash,
            MutationConfirmation.status == PENDING,
        )
        .order_by(MutationConfirmation.id.desc())
    )
    if pending_duplicate is not None:
        return _prepare_result(pending_duplicate)

    _supersede_pending(session, payload.patient_id)
    status = SUPERSEDED if _has_newer_general_message(session, payload.patient_id, origin_chat_id) else PENDING
    display = _nutrition_preference_display(request, fact)
    row = MutationConfirmation(
        public_id=uuid4().hex,
        patient_id=payload.patient_id,
        origin_request_notification_id=notification_id,
        conversation_id=conversation_id,
        origin_trace_id=payload.trace_id,
        origin_agent=payload.source_event_type,
        source_event_type=payload.source_event_type,
        action_type=action_type,
        action_name=payload.action_name,
        tool_call_id=payload.tool_call_id,
        arguments_json=dump_json(arguments),
        action_fingerprint=fingerprint,
        target_snapshot_json=dump_json(snapshot),
        target_snapshot_hash=snapshot_hash,
        display_json=dump_json(display),
        continuation_json=dump_json(_continuation_context(payload.request_context)),
        idempotency_key=idempotency_key,
        status=status,
        resolved_at=utc_now() if status == SUPERSEDED else None,
    )
    session.add(row)
    session.flush()
    return _prepare_result(row)


def _execute_nutrition_preference_mutation(
    session: Session,
    row: MutationConfirmation,
) -> ConfirmedMutationExecutionResult:
    arguments = parse_json_object(row.arguments_json)
    request = NutritionPreferenceFactRequest.model_validate(arguments)
    node, fact = _find_nutrition_preference_target(session, request)
    current_snapshot = _nutrition_preference_snapshot(node, fact)
    if _hash_payload(current_snapshot) != row.target_snapshot_hash:
        row.status = STALE
        row.error_message = "mutation_confirmation_snapshot_changed"
        row.resolved_at = utc_now()
        session.flush()
        return _execution_result(row)

    result = NutritionPreferenceFactResult.model_validate(
        record_preference_fact(
            session,
            patient_id=request.patient_id,
            predicate=request.predicate,
            object_label=request.object_label,
            object_type=request.object_type,
            strength=request.strength,
            safety_level=request.safety_level,
            confidence=request.confidence,
            source=request.source,
            evidence_text=request.evidence_text,
            source_trace_id=request.source_trace_id,
        )
    )
    if not result.success:
        row.status = FAILED
        row.error_message = "nutrition_preference_write_failed"
    else:
        row.status = APPLIED
        row.result_json = dump_json(result.model_dump(mode="json"))
        row.error_message = ""
    row.resolved_at = utc_now()
    session.flush()
    return _execution_result(row)


def _prepare_nutrition_preference_arguments(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
) -> tuple[
    dict,
    NutritionPreferenceFactRequest,
    NutritionOntologyNode | None,
    NutritionPatientPreferenceTriple | None,
]:
    request_data = {
        **payload.arguments,
        "patient_id": payload.patient_id,
        "object_label": str(payload.arguments.get("object_label") or "").strip(),
        "source": "agent_tool",
        "source_trace_id": payload.trace_id,
    }
    request = NutritionPreferenceFactRequest.model_validate(request_data)
    if not normalize_ontology_label(request.object_label):
        raise ValueError("nutrition_preference_object_label_required")
    node, fact = _find_nutrition_preference_target(session, request)
    return request.model_dump(mode="json"), request, node, fact


def _find_nutrition_preference_target(
    session: Session,
    request: NutritionPreferenceFactRequest,
) -> tuple[NutritionOntologyNode | None, NutritionPatientPreferenceTriple | None]:
    node_key = ontology_node_key(request.object_type, request.object_label)
    node = session.scalar(
        select(NutritionOntologyNode).where(NutritionOntologyNode.node_key == node_key)
    )
    if node is None:
        return None, None
    fact = session.scalar(
        select(NutritionPatientPreferenceTriple).where(
            NutritionPatientPreferenceTriple.patient_id == request.patient_id,
            NutritionPatientPreferenceTriple.predicate == request.predicate,
            NutritionPatientPreferenceTriple.object_node_id == node.id,
        )
    )
    return node, fact


def _nutrition_preference_snapshot(
    node: NutritionOntologyNode | None,
    fact: NutritionPatientPreferenceTriple | None,
) -> dict:
    return {
        "node": (
            {
                "id": node.id,
                "node_key": node.node_key,
                "node_type": node.node_type,
                "label": node.label,
                "normalized_label": node.normalized_label,
                "source": node.source,
                "metadata_json": node.metadata_json,
                "updated_at": node.updated_at.isoformat() if node.updated_at else None,
            }
            if node is not None
            else None
        ),
        "fact": (
            {
                "id": fact.id,
                "patient_id": fact.patient_id,
                "predicate": fact.predicate,
                "object_node_id": fact.object_node_id,
                "strength": fact.strength,
                "safety_level": fact.safety_level,
                "confidence": fact.confidence,
                "source": fact.source,
                "evidence_text": fact.evidence_text,
                "source_trace_id": fact.source_trace_id,
                "status": fact.status,
                "updated_at": fact.updated_at.isoformat() if fact.updated_at else None,
            }
            if fact is not None
            else None
        ),
    }


def _nutrition_preference_desired_state(request: NutritionPreferenceFactRequest) -> dict:
    safety_level = "hard" if request.predicate in HARD_CONSTRAINT_PREDICATES else "soft"
    if request.safety_level in {"hard", "soft"} and request.predicate not in HARD_CONSTRAINT_PREDICATES:
        safety_level = request.safety_level
    return {
        "patient_id": request.patient_id,
        "predicate": request.predicate,
        "object_key": ontology_node_key(request.object_type, request.object_label),
        "object_type": request.object_type,
        "strength": request.strength,
        "safety_level": safety_level,
        "confidence": request.confidence,
        "status": "active",
    }


def _nutrition_preference_is_applied(
    fact: NutritionPatientPreferenceTriple | None,
    desired_state: dict,
) -> bool:
    if fact is None:
        return False
    return (
        fact.status == desired_state["status"]
        and fact.safety_level == desired_state["safety_level"]
        and abs(float(fact.strength) - float(desired_state["strength"])) < 1e-9
        and abs(float(fact.confidence) - float(desired_state["confidence"])) < 1e-9
    )


def _nutrition_preference_display(
    request: NutritionPreferenceFactRequest,
    fact: NutritionPatientPreferenceTriple | None,
) -> dict:
    predicate_label = _nutrition_preference_predicate_label(request.predicate)
    object_label = request.object_label.strip()
    desired_state = _nutrition_preference_desired_state(request)
    safety_label = "\uac15\uc81c \uc81c\uc57d" if desired_state["safety_level"] == "hard" else "\uc77c\ubc18 \uc120\ud638"
    before = "\ub4f1\ub85d\ub418\uc9c0 \uc54a\uc74c"
    if fact is not None:
        status_label = "\ud65c\uc131" if fact.status == "active" else "\ube44\ud65c\uc131"
        before = (
            f"{_nutrition_preference_predicate_label(fact.predicate)} / "
            f"{_nutrition_preference_safety_label(fact.safety_level)} / "
            f"{status_label}"
        )
    if request.predicate == "allergic_to":
        question = f"{object_label} \uc54c\ub808\ub974\uae30\ub97c \uc601\uc591 \uc81c\uc57d \uc815\ubcf4\ub85c \uae30\ub85d\ud560\uae4c\uc694?"
    else:
        question = f"{object_label}\uc5d0 \ub300\ud55c {predicate_label} \uc815\ubcf4\ub97c \uae30\ub85d\ud560\uae4c\uc694?"
    return {
        "title": "\uc601\uc591 \uc81c\uc57d \uc815\ubcf4 \uae30\ub85d" if desired_state["safety_level"] == "hard" else "\uc601\uc591 \uc120\ud638\ub3c4 \uae30\ub85d",
        "question": question,
        "action_label": "\ubcc0\uacbd" if fact is not None else "\uae30\ub85d",
        "target": object_label,
        "details": [
            {"label": "\uc815\ubcf4 \uc720\ud615", "value": predicate_label},
            {"label": "\ub300\uc0c1 \uc720\ud615", "value": _nutrition_preference_object_type_label(request.object_type)},
            {"label": "\ubcc0\uacbd \uc804", "value": before},
            {"label": "\ubcc0\uacbd \ud6c4", "value": f"{predicate_label} / {safety_label} / \ud65c\uc131"},
        ],
    }


def _nutrition_preference_predicate_label(predicate: str) -> str:
    return {
        "likes": "\uc120\ud638",
        "dislikes": "\ube44\uc120\ud638",
        "prefers": "\uc120\ud638 \uacbd\ud5a5",
        "avoids_by_preference": "\uae30\ud638\uc0c1 \ud68c\ud53c",
        "cannot_consume": "\uc12d\ucde8 \ubd88\uac00",
        "allergic_to": "\uc54c\ub808\ub974\uae30",
        "medically_avoids": "\uc758\ud559\uc801 \uc81c\ud55c",
        "religious_avoids": "\uc885\uad50\u00b7\uc2e0\ub150 \uc81c\ud55c",
    }.get(predicate, predicate)


def _nutrition_preference_object_type_label(object_type: str) -> str:
    return {
        "food": "\uc74c\uc2dd",
        "ingredient": "\uc2dd\uc7ac\ub8cc",
        "food_category": "\uc74c\uc2dd \ubd84\ub958",
        "cuisine": "\uc694\ub9ac \uc720\ud615",
        "preparation": "\uc870\ub9ac\ubc95",
        "nutrient": "\uc601\uc591\uc18c",
        "nutrient_risk": "\uc601\uc591 \uc704\ud5d8",
        "restriction": "\uc81c\ud55c \uc0ac\ud56d",
        "diet_style": "\uc2dd\ub2e8 \uc720\ud615",
    }.get(object_type, object_type)


def _nutrition_preference_safety_label(safety_level: str) -> str:
    return "\uac15\uc81c \uc81c\uc57d" if safety_level == "hard" else "\uc77c\ubc18 \uc120\ud638"



def _prepare_dose_arguments(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
) -> tuple[dict, DoseEvent]:
    request_data = {
        **payload.arguments,
        "source_trace_id": payload.trace_id,
        "source_event_type": payload.source_event_type,
    }
    request = DoseTakenToolRequest.model_validate(request_data)
    event = session.get(DoseEvent, request.dose_event_id)
    if event is None or event.patient_id != payload.patient_id:
        raise ValueError("mutation_confirmation_target_not_found")
    if request.taken_at is None:
        current_time = payload.request_context.get("current_time")
        if isinstance(current_time, str) and current_time:
            request.taken_at = datetime.fromisoformat(current_time)
        else:
            request.taken_at = ensure_clock(session).current_time
    return request.model_dump(mode="json"), event


def _origin_context(session: Session, context: dict) -> tuple[int | None, str, int | None]:
    callback = context.get("callback_context")
    callback = callback if isinstance(callback, dict) else {}
    notification_id = callback.get("notification_id")
    notification_id = int(notification_id) if notification_id is not None else None
    conversation_id = str(callback.get("conversation_id") or "")
    origin_chat_id = None
    notification = session.get(Notification, notification_id) if notification_id is not None else None
    if notification is not None:
        metadata = parse_json_object(notification.metadata_json)
        conversation_id = conversation_id or str(metadata.get("agent_conversation_id") or "")
        raw_chat_id = metadata.get("chat_message_id")
        origin_chat_id = int(raw_chat_id) if raw_chat_id is not None else None
    return notification_id, conversation_id, origin_chat_id


def _continuation_context(context: dict) -> dict:
    return {
        "message": context.get("message"),
        "event_type": context.get("event_type"),
        "current_time": context.get("current_time"),
        "callback_context": context.get("callback_context"),
        "request_metadata": context.get("request_metadata"),
    }


def _has_newer_general_message(
    session: Session,
    patient_id: str,
    origin_chat_id: int | None,
) -> bool:
    if origin_chat_id is None:
        return False
    return (
        session.scalar(
            select(ChatMessage.id)
            .where(
                ChatMessage.patient_id == patient_id,
                ChatMessage.sender_type == "patient",
                ChatMessage.id > origin_chat_id,
                ChatMessage.category.notin_(
                    {
                        "ae_response",
                        "mutation_confirmation",
                        "policy_confirmation",
                        "side_effect_reminder_safety",
                    }
                ),
            )
            .limit(1)
        )
        is not None
    )


def _supersede_pending(session: Session, patient_id: str) -> int:
    rows = list(
        session.scalars(
            select(MutationConfirmation).where(
                MutationConfirmation.patient_id == patient_id,
                MutationConfirmation.status == PENDING,
            )
        ).all()
    )
    for row in rows:
        row.status = SUPERSEDED
        row.resolved_at = utc_now()
        mirror_confirmation_card_status(session, row)
    if rows:
        session.flush()
    return len(rows)


def _prepare_result(row: MutationConfirmation) -> MutationConfirmationPrepareResult:
    return MutationConfirmationPrepareResult(
        confirmation_required=row.status == PENDING,
        confirmation_id=row.public_id,
        action_type=row.action_type,
        action_name=row.action_name,
        tool_call_id=row.tool_call_id,
        action_fingerprint=row.action_fingerprint,
        status=row.status,
        display=parse_json_object(row.display_json),
        execution_result=parse_json_object(row.result_json),
    )


def _execution_result(row: MutationConfirmation) -> ConfirmedMutationExecutionResult:
    status = row.status if row.status in {APPLIED, STALE, FAILED} else FAILED
    return ConfirmedMutationExecutionResult(
        confirmation_id=row.public_id,
        action_name=row.action_name,
        status=status,
        tool_result=parse_json_object(row.result_json),
        error=row.error_message,
    )


def _dose_snapshot(event: DoseEvent | None) -> dict:
    if event is None:
        return {}
    return {
        "dose_event_id": event.id,
        "patient_id": event.patient_id,
        "medication_name": event.medication_name,
        "slot_label": event.slot_label,
        "scheduled_for": event.scheduled_for.isoformat(),
        "status": event.status,
        "taken_at": event.taken_at.isoformat() if event.taken_at else None,
    }


def _dose_fingerprint_arguments(arguments: dict) -> dict:
    return {
        key: value
        for key, value in arguments.items()
        if key not in {"source_trace_id", "source_event_type"}
    }


def _dose_status_label(status: str) -> str:
    return {
        "scheduled": "\ubcf5\uc57d \uc608\uc815",
        "missed": "\ubbf8\ubcf5\uc6a9",
        "taken": "\ubcf5\uc57d \uc644\ub8cc",
    }.get(status, status)


def _hash_payload(value: dict) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
