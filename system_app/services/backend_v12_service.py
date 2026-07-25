from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_app.integration.contracts import (
    ContractError,
    MedicationDoseEventMutationPayload,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    NotificationPolicyChangeResult,
    NutritionFoodMutationPayload,
    NutritionMealMutationPayload,
    RecordChangeRequest,
    RecordChangeResponse,
    RecordChangeResult,
)
from shared.json_utils import dump_json, parse_json_object
from shared.time_utils import utc_now
from system_app.models import (
    BackendApiRequest,
    ChatMessage,
    DoseEvent,
    NutritionFood,
    NutritionMeal,
    ReminderPolicy,
)
from system_app.services.dose_event_service import mark_dose_taken_command
from system_app.services.nutrition_service import (
    delete_food,
    delete_meal,
    record_meal,
    update_food,
    update_meal,
)

RECORD_CHANGE_PATH = "/agent/sync/record-change"
POLICY_CHANGE_PATH = "/agent/sync/notification-policy-change"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class BackendReplay:
    status_code: int
    body: dict[str, Any]


class BackendRequestConflict(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class BackendRequestGate:
    def begin(self, session: Session, *, api_path: str, request_id: str, payload: Any) -> BackendReplay | None:
        request_hash = _payload_hash(payload)
        row = session.scalar(
            select(BackendApiRequest).where(
                BackendApiRequest.api_path == api_path,
                BackendApiRequest.request_id == request_id,
            )
        )
        if row is not None:
            if row.request_hash != request_hash:
                raise BackendRequestConflict(
                    "IDEMPOTENCY_CONFLICT",
                    "The request_id was already used with a different request body.",
                )
            if row.status == COMPLETED:
                return BackendReplay(
                    status_code=row.http_status,
                    body=parse_json_object(row.response_json),
                )
            raise BackendRequestConflict(
                "REQUEST_IN_PROGRESS",
                "The same request is already being processed.",
            )

        session.add(
            BackendApiRequest(
                api_path=api_path,
                request_id=request_id,
                request_hash=request_hash,
                status=PROCESSING,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )
        session.flush()
        return None

    def complete(
        self,
        session: Session,
        *,
        api_path: str,
        request_id: str,
        status_code: int,
        body: dict[str, Any],
        error_code: str = "",
    ) -> None:
        row = session.scalar(
            select(BackendApiRequest).where(
                BackendApiRequest.api_path == api_path,
                BackendApiRequest.request_id == request_id,
            )
        )
        if row is None:
            raise RuntimeError("backend_idempotency_request_not_started")
        row.status = COMPLETED
        row.http_status = status_code
        row.response_json = dump_json(body)
        row.error_code = error_code
        row.updated_at = utc_now()
        session.flush()


def apply_record_change(session: Session, request: RecordChangeRequest) -> RecordChangeResponse:
    context_error = _validate_confirmation_context(
        session,
        patient_id=request.patient_id,
        conversation_id=request.conversation_id,
        confirmation_message_id=request.confirmation_message_id,
        source_chat_request_id=request.source_chat_request_id,
    )
    if context_error is not None:
        return _record_failure(request, context_error, retryable=False)

    try:
        result = _mutate_record(session, request)
    except VersionConflict as exc:
        return _record_failure(
            request,
            "VERSION_CONFLICT",
            retryable=False,
            details={"expected_version": exc.expected, "current_version": exc.actual},
        )
    except ValueError as exc:
        return _record_failure(request, str(exc), retryable=False)
    return RecordChangeResponse(
        success=True,
        request_id=request.request_id,
        result=result,
        error=None,
        processed_at=datetime.now(UTC),
    )


def apply_notification_policy_change(
    session: Session,
    request: NotificationPolicyChangeRequest,
) -> NotificationPolicyChangeResponse:
    context_error = _validate_confirmation_context(
        session,
        patient_id=request.patient_id,
        conversation_id=request.conversation_id,
        confirmation_message_id=request.confirmation_message_id,
        source_chat_request_id=request.source_chat_request_id,
    )
    if context_error is not None:
        return _policy_failure(request, context_error, retryable=False)

    try:
        policy_id = _positive_int(request.policy_id, "notification_policy_not_found")
        policy = session.scalar(
            select(ReminderPolicy).where(
                ReminderPolicy.id == policy_id,
                ReminderPolicy.patient_id == request.patient_id,
            )
        )
        if policy is None:
            raise ValueError("notification_policy_not_found")
        _require_version(policy.version, request.expected_version)

        applied = request.payload.decision == "apply"
        if applied:
            changes = request.payload.changes
            if changes is None:
                raise ValueError("policy_changes_empty")
            for field_name in changes.model_fields_set:
                value = getattr(changes, field_name)
                if value is not None:
                    setattr(policy, field_name, value)
            policy.reason = request.payload.reason
            policy.source = "agent_v1.2"
            policy.version += 1
            policy.updated_at = utc_now()
            session.flush()

        return NotificationPolicyChangeResponse(
            success=True,
            request_id=request.request_id,
            result=NotificationPolicyChangeResult(
                policy_id=str(policy.id),
                decision=request.payload.decision,
                applied=applied,
                version=policy.version,
            ),
            error=None,
            processed_at=datetime.now(UTC),
        )
    except VersionConflict as exc:
        return _policy_failure(
            request,
            "VERSION_CONFLICT",
            retryable=False,
            details={"expected_version": exc.expected, "current_version": exc.actual},
        )
    except ValueError as exc:
        return _policy_failure(request, str(exc), retryable=False)


class VersionConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__("VERSION_CONFLICT")
        self.expected = expected
        self.actual = actual


def _mutate_record(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    if request.resource_type == "nutrition_meal":
        return _mutate_meal(session, request)
    if request.resource_type == "nutrition_food":
        return _mutate_food(session, request)
    if request.resource_type == "medication_dose_event":
        return _mutate_dose_event(session, request)
    raise ValueError("unsupported_resource_type")


def _mutate_meal(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    if request.operation == "create":
        payload = _meal_payload(request)
        created = record_meal(
            session,
            patient_id=request.patient_id,
            foods=[item.model_dump(mode="python") for item in payload.foods or []],
            meal_type=str(payload.meal_type),
            meal_date=payload.meal_date,
            meal_time=payload.meal_time,
            scenario_key=payload.scenario_key or "",
            description=payload.description or "",
        )
        meal_id = int(created["meal"]["id"])
        meal = session.get(NutritionMeal, meal_id)
        if meal is None:
            raise RuntimeError("created_nutrition_meal_missing")
        meal.version = max(1, meal.version)
        meal.updated_at = utc_now()
        session.flush()
        return RecordChangeResult(
            resource_type=request.resource_type,
            operation=request.operation,
            record_id=str(meal.id),
            version=meal.version,
        )

    meal_id = _positive_int(request.record_id, "nutrition_meal_not_found")
    meal = session.scalar(
        select(NutritionMeal).where(
            NutritionMeal.id == meal_id,
            NutritionMeal.patient_id == request.patient_id,
        )
    )
    if meal is None:
        raise ValueError("nutrition_meal_not_found")
    _require_version(meal.version, request.expected_version)
    next_version = meal.version + 1
    if request.operation == "delete":
        delete_meal(
            session,
            meal_id=meal.id,
            patient_id=request.patient_id,
            reason="agent_v1.2",
        )
    else:
        payload = _meal_payload(request)
        update_meal(
            session,
            meal_id=meal.id,
            patient_id=request.patient_id,
            foods=[item.model_dump(mode="python") for item in payload.foods] if payload.foods is not None else None,
            meal_type=payload.meal_type,
            meal_date=payload.meal_date,
            meal_time=payload.meal_time,
            scenario_key=payload.scenario_key,
            description=payload.description,
            reason=payload.reason,
        )
        meal.version = next_version
        meal.updated_at = utc_now()
        session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=str(meal_id),
        version=next_version,
    )


def _mutate_food(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    meal_id = _positive_int(request.parent_record_id, "nutrition_meal_not_found")
    food_id = _positive_int(request.record_id, "nutrition_food_not_found")
    food = session.scalar(
        select(NutritionFood)
        .join(NutritionMeal, NutritionMeal.id == NutritionFood.meal_id)
        .where(
            NutritionFood.id == food_id,
            NutritionFood.meal_id == meal_id,
            NutritionMeal.patient_id == request.patient_id,
        )
    )
    if food is None:
        raise ValueError("nutrition_food_not_found")
    _require_version(food.version, request.expected_version)
    next_version = food.version + 1
    if request.operation == "delete":
        delete_food(
            session,
            meal_id=meal_id,
            food_id=food_id,
            patient_id=request.patient_id,
            reason="agent_v1.2",
            delete_empty_meal=True,
        )
    else:
        payload = request.payload
        if not isinstance(payload, NutritionFoodMutationPayload):
            raise ValueError("nutrition_food_payload_required")
        update_food(
            session,
            meal_id=meal_id,
            food_id=food_id,
            patient_id=request.patient_id,
            food_ref_id=payload.food_ref_id,
            food_name=payload.food_name,
            portion=payload.portion,
            nutrients=payload.nutrients,
            reason=payload.reason,
        )
        food.version = next_version
        food.updated_at = utc_now()
        session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=str(food_id),
        parent_record_id=str(meal_id),
        version=next_version,
    )


def _mutate_dose_event(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    event_id = _positive_int(request.record_id, "medication_dose_event_not_found")
    event = session.scalar(
        select(DoseEvent).where(
            DoseEvent.id == event_id,
            DoseEvent.patient_id == request.patient_id,
        )
    )
    if event is None:
        raise ValueError("medication_dose_event_not_found")
    _require_version(event.version, request.expected_version)
    payload = request.payload
    if not isinstance(payload, MedicationDoseEventMutationPayload):
        raise ValueError("medication_dose_event_payload_required")
    event = mark_dose_taken_command(session, event.id, taken_at=payload.taken_at)
    if event is None:
        raise ValueError("medication_dose_event_not_found")
    event.version += 1
    event.updated_at = utc_now()
    session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=str(event.id),
        version=event.version,
    )


def _validate_confirmation_context(
    session: Session,
    *,
    patient_id: str,
    conversation_id: str,
    confirmation_message_id: str,
    source_chat_request_id: str,
) -> str | None:
    try:
        message_id = int(confirmation_message_id)
    except (TypeError, ValueError):
        return "CONFIRMATION_MESSAGE_NOT_FOUND"
    confirmation = session.scalar(
        select(ChatMessage).where(
            ChatMessage.id == message_id,
            ChatMessage.patient_id == patient_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "user",
        )
    )
    if confirmation is None:
        return "CONFIRMATION_MESSAGE_NOT_FOUND"
    source_exists = session.scalar(
        select(ChatMessage.id).where(
            ChatMessage.patient_id == patient_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.ai_request_id == source_chat_request_id,
        )
    )
    if source_exists is None:
        return "SOURCE_CHAT_REQUEST_NOT_FOUND"
    return None


def _meal_payload(request: RecordChangeRequest) -> NutritionMealMutationPayload:
    if not isinstance(request.payload, NutritionMealMutationPayload):
        raise ValueError("nutrition_meal_payload_required")
    return request.payload


def _require_version(actual: int, expected: int | None) -> None:
    if expected is None:
        raise ValueError("expected_version_required")
    if actual != expected:
        raise VersionConflict(expected, actual)


def _positive_int(value: str | None, error_code: str) -> int:
    try:
        parsed = int(value or "")
    except ValueError as exc:
        raise ValueError(error_code) from exc
    if parsed <= 0:
        raise ValueError(error_code)
    return parsed


def _record_failure(
    request: RecordChangeRequest,
    code: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> RecordChangeResponse:
    return RecordChangeResponse(
        success=False,
        request_id=request.request_id,
        result=None,
        error=ContractError(code=code, message=code, retryable=retryable, details=details),
        processed_at=datetime.now(UTC),
    )


def _policy_failure(
    request: NotificationPolicyChangeRequest,
    code: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> NotificationPolicyChangeResponse:
    return NotificationPolicyChangeResponse(
        success=False,
        request_id=request.request_id,
        result=None,
        error=ContractError(code=code, message=code, retryable=retryable, details=details),
        processed_at=datetime.now(UTC),
    )


def _payload_hash(payload: Any) -> str:
    if hasattr(payload, "model_dump"):
        value = payload.model_dump(mode="json")
    else:
        value = payload
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
