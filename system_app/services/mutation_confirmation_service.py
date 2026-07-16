from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from agent_app.confirmation_actions import ConfirmationActionRegistry
from agent_app.tool_names import UPDATE_MEDICATION_DOSE_EVENT_STATUS
from shared.json_utils import dump_json, parse_json_object
from shared.schemas import (
    ConfirmedMutationExecutionRequest,
    ConfirmedMutationExecutionResult,
    DoseTakenToolRequest,
    DoseTakenToolResult,
    MutationConfirmationPrepareRequest,
    MutationConfirmationPrepareResult,
)
from shared.time_utils import utc_now
from system_app.models import ChatMessage, DoseEvent, MutationConfirmation, Notification
from system_app.services.agent_callback_service import apply_agent_dose_taken_request
from system_app.services.clock_service import ensure_clock

PENDING = "pending"
EXECUTING = "executing"
APPLIED = "applied"
CANCELLED = "cancelled"
SUPERSEDED = "superseded"
STALE = "stale"
FAILED = "failed"
EXECUTION_LEASE = timedelta(minutes=2)


def prepare_mutation_confirmation(
    session: Session,
    payload: MutationConfirmationPrepareRequest,
) -> MutationConfirmationPrepareResult:
    action = ConfirmationActionRegistry.get(payload.action_name)
    if action is None or not ConfirmationActionRegistry.requires_confirmation(payload.action_name):
        raise ValueError("mutation_confirmation_action_not_enabled")
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
