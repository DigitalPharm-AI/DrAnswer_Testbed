from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.backend_v13_contracts import (
    CommonErrorResponse,
    ContractError,
    MedicationDoseEventMutationPayload,
    MedicationSideEffectMutationPayload,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    NotificationPolicyChangeResult,
    POLICY_MAX_EFFECTIVE_DAYS,
    NutritionFoodMutationPayload,
    NutritionMealMutationPayload,
    RecordChangeRequest,
    RecordChangeResponse,
    RecordChangeResult,
)
from shared.contract_errors import contract_error_definition
from shared.json_utils import dump_json, parse_json_object
from shared.time_utils import utc_now
from system_app.models import (
    BackendApiRequest,
    ChatMessage,
    DoseEvent,
    NutritionFood,
    NutritionMeal,
    ReminderPolicy,
    SideEffectRecord,
)
from system_app.services.dose_event_service import mark_dose_taken_command
from system_app.services.nutrition_service import (
    delete_food,
    delete_meal,
    record_meal,
    update_food,
    update_meal,
)
from system_app.services.policy_service import (
    policy_boundary_violations,
    resolve_policy_boundary_for_slot,
)
from shared.settings import get_settings

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
            return self._existing_result(row, request_hash=request_hash)

        try:
            # The database unique constraint is the cross-replica arbiter.
            # A savepoint keeps the outer request transaction usable after a
            # concurrent insert loses the race.
            with session.begin_nested():
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
        except IntegrityError:
            winner = session.scalar(
                select(BackendApiRequest).where(
                    BackendApiRequest.api_path == api_path,
                    BackendApiRequest.request_id == request_id,
                )
            )
            if winner is None:
                raise
            return self._existing_result(
                winner,
                request_hash=request_hash,
            )
        return None

    @staticmethod
    def _existing_result(
        row: BackendApiRequest,
        *,
        request_hash: str,
    ) -> BackendReplay | None:
        if row.request_hash != request_hash:
            raise BackendRequestConflict(
                "IDEMPOTENCY_CONFLICT",
                "The request_id was reused with a different request body.",
            )
        if row.status == COMPLETED:
            return BackendReplay(
                status_code=row.http_status,
                body=parse_json_object(row.response_json),
            )
        raise BackendRequestConflict(
            "REQUEST_IN_PROGRESS",
            "The same request_id is currently being processed.",
        )

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


def apply_record_change(
    session: Session,
    request: RecordChangeRequest,
) -> RecordChangeResponse | CommonErrorResponse:
    context_error, confirmation = _validate_record_confirmation_binding(
        session,
        request,
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
        return _record_failure(
            request,
            _record_public_error_code(exc),
            retryable=False,
        )
    response = RecordChangeResponse(
        request_id=request.request_id,
        result=result,
        processed_at=datetime.now(UTC),
    )
    return response


def apply_notification_policy_change(
    session: Session,
    request: NotificationPolicyChangeRequest,
) -> NotificationPolicyChangeResponse | CommonErrorResponse:
    context_error, confirmation = _validate_policy_confirmation_binding(
        session,
        request,
    )
    if context_error is not None:
        return _policy_failure(request, context_error, retryable=False)

    try:
        policy = session.scalar(
            select(ReminderPolicy).where(
                ReminderPolicy.public_id == request.policy_id,
                ReminderPolicy.patient_id == request.patient_id,
            ).with_for_update()
        )
        if policy is None:
            raise ValueError("notification_policy_not_found")
        _require_version(policy.version, request.expected_version)

        applied = request.payload.decision == "apply"
        if applied:
            changes = request.payload.changes
            if changes is None:
                raise ValueError("policy_changes_empty")
            _validate_policy_changes(session, request, policy)
            for field_name in changes.model_fields_set:
                value = getattr(changes, field_name)
                if value is not None:
                    setattr(policy, field_name, value)
            policy.reason = request.payload.reason
            policy.source = "agent_v1.3"
            policy.version += 1
            policy.updated_at = utc_now()
            session.flush()

    except VersionConflict as exc:
        return _policy_failure(
            request,
            "VERSION_CONFLICT",
            retryable=False,
            details={"expected_version": exc.expected, "current_version": exc.actual},
        )
    except PolicyValidationError as exc:
        return _policy_failure(
            request,
            exc.code,
            retryable=False,
            details=exc.details,
        )
    except ValueError as exc:
        return _policy_failure(
            request,
            _policy_public_error_code(exc),
            retryable=False,
        )
    response = NotificationPolicyChangeResponse(
        request_id=request.request_id,
        result=NotificationPolicyChangeResult(
            policy_id=policy.public_id,
            decision=request.payload.decision,
            version=policy.version,
        ),
        processed_at=datetime.now(UTC),
    )
    return response


def _validate_policy_changes(
    session: Session,
    request: NotificationPolicyChangeRequest,
    policy: ReminderPolicy,
) -> None:
    changes = request.payload.changes
    if changes is None:
        raise ValueError("policy_changes_empty")
    values = {
        "extra_reminders": (
            changes.extra_reminders
            if changes.extra_reminders is not None
            else policy.extra_reminders
        ),
        "interval_minutes": (
            changes.interval_minutes
            if changes.interval_minutes is not None
            else policy.interval_minutes
        ),
        "missed_dose_after_minutes": (
            changes.missed_dose_after_minutes
            if changes.missed_dose_after_minutes is not None
            else policy.missed_dose_after_minutes or get_settings().missed_dose_grace_minutes
        ),
        "primary_reminder_timing": (
            changes.primary_reminder_timing
            if changes.primary_reminder_timing is not None
            else policy.primary_reminder_timing
        ),
        "primary_reminder_offset_minutes": (
            changes.primary_reminder_offset_minutes
            if changes.primary_reminder_offset_minutes is not None
            else policy.primary_reminder_offset_minutes
        ),
    }
    effective_start_date = changes.effective_start_date or policy.effective_start_date
    effective_end_date = changes.effective_end_date or policy.effective_end_date
    if effective_end_date < effective_start_date:
        raise PolicyValidationError(
            "POLICY_EFFECTIVE_DATE_RANGE_INVALID",
            {
                "effective_start_date": effective_start_date.isoformat(),
                "effective_end_date": effective_end_date.isoformat(),
            },
        )
    if (effective_end_date - effective_start_date).days + 1 > POLICY_MAX_EFFECTIVE_DAYS:
        raise PolicyValidationError(
            "POLICY_EFFECTIVE_DATE_RANGE_TOO_LARGE",
            {"maximum_effective_days": POLICY_MAX_EFFECTIVE_DAYS},
        )
    if (
        "effective_start_date" in changes.model_fields_set
        and effective_start_date < request.requested_at.date()
    ):
        raise PolicyValidationError(
            "POLICY_EFFECTIVE_START_IN_PAST",
            {
                "effective_start_date": effective_start_date.isoformat(),
                "request_date": request.requested_at.date().isoformat(),
            },
        )
    boundary = resolve_policy_boundary_for_slot(
        session,
        request.patient_id,
        policy.slot_label,
        effective_start_date,
    )
    violations = policy_boundary_violations(values, boundary)
    if violations:
        raise PolicyValidationError(
            "POLICY_BOUNDARY_VIOLATION",
            {
                "policy_id": policy.public_id,
                "boundary_key": boundary.boundary_key,
                "violations": violations,
            },
        )


class VersionConflict(RuntimeError):
    def __init__(self, expected: int, actual: int) -> None:
        super().__init__("VERSION_CONFLICT")
        self.expected = expected
        self.actual = actual


class PolicyValidationError(ValueError):
    def __init__(self, code: str, details: dict[str, Any]) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _mutate_record(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    if request.resource_type == "nutrition_meal":
        return _mutate_meal(session, request)
    if request.resource_type == "nutrition_food":
        return _mutate_food(session, request)
    if request.resource_type == "medication_dose_event":
        return _mutate_dose_event(session, request)
    if request.resource_type == "medication_side_effect":
        return _mutate_side_effect(session, request)
    raise ValueError("unsupported_resource_type")


def _mutate_meal(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    if request.operation == "create":
        payload = _meal_payload(request)
        created = record_meal(
            session,
            patient_id=request.patient_id,
            foods=[
                item.model_dump(mode="python", exclude_none=True)
                for item in payload.foods or []
            ],
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
            record_id=meal.public_id,
            version=meal.version,
        )

    meal = _meal_by_external_id(
        session,
        patient_id=request.patient_id,
        external_id=request.record_id,
        for_update=True,
    )
    if meal is None:
        raise ValueError("nutrition_meal_not_found")
    meal_public_id = meal.public_id
    _require_version(meal.version, request.expected_version)
    next_version = meal.version + 1
    if request.operation == "delete":
        delete_meal(
            session,
            meal_id=meal.id,
            patient_id=request.patient_id,
            reason="agent_v1.3",
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
            reason=payload.reason or "",
        )
        meal.version = next_version
        meal.updated_at = utc_now()
        session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=meal_public_id,
        version=next_version,
    )


def _mutate_food(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    meal = _meal_by_external_id(
        session,
        patient_id=request.patient_id,
        external_id=request.parent_record_id,
        for_update=True,
    )
    if meal is None:
        raise ValueError("nutrition_meal_not_found")
    food = _food_by_external_id(
        session,
        meal=meal,
        external_id=request.record_id,
        for_update=True,
    )
    if food is None:
        raise ValueError("nutrition_food_not_found")
    food_public_id = food.public_id
    meal_public_id = meal.public_id
    _require_version(food.version, request.expected_version)
    next_version = food.version + 1
    if request.operation == "delete":
        delete_food(
            session,
            meal_id=meal.id,
            food_id=food.id,
            patient_id=request.patient_id,
            reason="agent_v1.3",
            delete_empty_meal=True,
        )
    else:
        payload = request.payload
        if not isinstance(payload, NutritionFoodMutationPayload):
            raise ValueError("nutrition_food_payload_required")
        update_food(
            session,
            meal_id=meal.id,
            food_id=food.id,
            patient_id=request.patient_id,
            food_ref_id=payload.food_ref_id,
            food_name=payload.food_name,
            portion=payload.portion,
            nutrients=(
                payload.nutrients.model_dump(
                    mode="python",
                    exclude_none=True,
                )
                if payload.nutrients is not None
                else None
            ),
        )
        food.version = next_version
        food.updated_at = utc_now()
        session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=food_public_id,
        parent_record_id=meal_public_id,
        version=next_version,
    )


def _mutate_dose_event(session: Session, request: RecordChangeRequest) -> RecordChangeResult:
    event = _dose_event_by_external_id(
        session,
        patient_id=request.patient_id,
        external_id=request.record_id,
        for_update=True,
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
        record_id=event.public_id,
        version=event.version,
    )


def _mutate_side_effect(
    session: Session,
    request: RecordChangeRequest,
) -> RecordChangeResult:
    payload = request.payload
    if request.operation != "create" or not isinstance(
        payload,
        MedicationSideEffectMutationPayload,
    ):
        raise ValueError("medication_side_effect_payload_required")

    related_event = None
    if payload.related_dose_event_id:
        related_event = _dose_event_by_external_id(
            session,
            patient_id=request.patient_id,
            external_id=payload.related_dose_event_id,
        )
        if related_event is None:
            raise ValueError("related_medication_dose_event_not_found")

    record = SideEffectRecord(
        patient_id=request.patient_id,
        medication_name=payload.medication_name
        or (related_event.medication_name if related_event is not None else ""),
        symptom_text=payload.symptom_text,
        symptom_onset_text=payload.symptom_onset_text or "",
        suspected=payload.suspected,
        severity_result_json=dump_json(
            payload.severity.model_dump(mode="json")
        ),
        matched_effects_json=dump_json(payload.matched_effects or []),
        matched_items_json=dump_json(payload.matched_items or []),
        evidence="",
        recommendation="",
        # Agent execution traces remain inside the AI Server boundary.
        source_event_type="medication_agent",
        related_dose_event_id=related_event.id if related_event is not None else None,
        metadata_json="{}",
        version=1,
        updated_at=utc_now(),
    )
    session.add(record)
    session.flush()
    return RecordChangeResult(
        resource_type=request.resource_type,
        operation=request.operation,
        record_id=record.public_id,
        version=record.version,
    )


def _validate_confirmation_context(
    session: Session,
    *,
    patient_id: str,
    confirmation_message_id: str,
    source_chat_request_id: str,
) -> str | None:
    confirmation = _chat_message_by_external_id(
        session,
        patient_id=patient_id,
        external_id=confirmation_message_id,
        role="user",
    )
    if confirmation is None:
        return "CONFIRMATION_MESSAGE_NOT_FOUND"
    if confirmation.ai_request_id != source_chat_request_id:
        return "SOURCE_CHAT_REQUEST_NOT_FOUND"
    if confirmation.reply_to_message_id is None:
        return "CONFIRMATION_MESSAGE_NOT_FOUND"
    source = session.get(ChatMessage, confirmation.reply_to_message_id)
    if (
        source is None
        or source.patient_id != patient_id
        or source.role != "assistant"
        or source.message_type != "selection_box"
    ):
        return "CONFIRMATION_MESSAGE_NOT_FOUND"
    source_payload = parse_json_object(source.message_payload_json)
    selections = {
        str(value).strip()
        for value in (source_payload.get("selections") or [])
        if str(value).strip()
    }
    selected = str(confirmation.content or "").strip()
    if (
        selected == "취소"
        or selected not in selections
        or "취소" not in selections
    ):
        return "BUSINESS_VALIDATION_FAILED"
    return None


def _validate_record_confirmation_binding(
    session: Session,
    request: RecordChangeRequest,
) -> tuple[str | None, None]:
    error = _validate_confirmation_context(
        session,
        patient_id=request.patient_id,
        confirmation_message_id=request.confirmation_message_id,
        source_chat_request_id=request.source_chat_request_id,
    )
    return error, None


def _validate_policy_confirmation_binding(
    session: Session,
    request: NotificationPolicyChangeRequest,
) -> tuple[str | None, None]:
    error = _validate_confirmation_context(
        session,
        patient_id=request.patient_id,
        confirmation_message_id=request.confirmation_message_id,
        source_chat_request_id=request.source_chat_request_id,
    )
    return error, None


def _meal_payload(request: RecordChangeRequest) -> NutritionMealMutationPayload:
    if not isinstance(request.payload, NutritionMealMutationPayload):
        raise ValueError("nutrition_meal_payload_required")
    return request.payload


def _require_version(actual: int, expected: int | None) -> None:
    if expected is None:
        raise ValueError("expected_version_required")
    if actual != expected:
        raise VersionConflict(expected, actual)


def _chat_message_by_external_id(
    session: Session,
    *,
    patient_id: str,
    external_id: str | None,
    role: str | None = None,
) -> ChatMessage | None:
    predicates = [
        ChatMessage.patient_id == patient_id,
    ]
    if role is not None:
        predicates.append(ChatMessage.role == role)
    value = str(external_id or "").strip()
    if not value:
        return None
    message = session.scalar(
        select(ChatMessage).where(ChatMessage.public_id == value, *predicates)
    )
    return message


def _meal_by_external_id(
    session: Session,
    *,
    patient_id: str,
    external_id: str | None,
    for_update: bool = False,
) -> NutritionMeal | None:
    value = str(external_id or "").strip()
    if not value:
        return None
    statement = select(NutritionMeal).where(
        NutritionMeal.public_id == value,
        NutritionMeal.patient_id == patient_id,
    )
    if for_update:
        statement = statement.with_for_update()
    meal = session.scalar(statement)
    return meal


def _food_by_external_id(
    session: Session,
    *,
    meal: NutritionMeal,
    external_id: str | None,
    for_update: bool = False,
) -> NutritionFood | None:
    value = str(external_id or "").strip()
    if not value:
        return None
    statement = select(NutritionFood).where(
        NutritionFood.public_id == value,
        NutritionFood.meal_id == meal.id,
    )
    if for_update:
        statement = statement.with_for_update()
    food = session.scalar(statement)
    return food


def _dose_event_by_external_id(
    session: Session,
    *,
    patient_id: str,
    external_id: str | None,
    for_update: bool = False,
) -> DoseEvent | None:
    value = str(external_id or "").strip()
    if not value:
        return None
    statement = select(DoseEvent).where(
        DoseEvent.public_id == value,
        DoseEvent.patient_id == patient_id,
    )
    if for_update:
        statement = statement.with_for_update()
    event = session.scalar(statement)
    return event


def _record_failure(
    request: RecordChangeRequest,
    code: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> CommonErrorResponse:
    definition = contract_error_definition(code)
    return CommonErrorResponse(
        request_id=request.request_id,
        error=ContractError(
            code=code,
            message=definition.message,
            retryable=retryable,
            details=details,
        ),
    )


def _policy_failure(
    request: NotificationPolicyChangeRequest,
    code: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> CommonErrorResponse:
    definition = contract_error_definition(code)
    message = (
        "The notification policy changed after it was read."
        if code == "VERSION_CONFLICT"
        else definition.message
    )
    return CommonErrorResponse(
        request_id=request.request_id,
        error=ContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        ),
    )


_RECORD_VALUE_ERROR_CODES = {
    "nutrition_meal_not_found": "NUTRITION_MEAL_NOT_FOUND",
    "nutrition_food_not_found": "NUTRITION_FOOD_NOT_FOUND",
    "medication_dose_event_not_found": "MEDICATION_DOSE_EVENT_NOT_FOUND",
    "related_medication_dose_event_not_found": (
        "RELATED_MEDICATION_DOSE_EVENT_NOT_FOUND"
    ),
}


def _record_public_error_code(exc: ValueError) -> str:
    return _RECORD_VALUE_ERROR_CODES.get(
        str(exc),
        "BUSINESS_VALIDATION_FAILED",
    )


def _policy_public_error_code(exc: ValueError) -> str:
    if str(exc) == "notification_policy_not_found":
        return "NOTIFICATION_POLICY_NOT_FOUND"
    return "BUSINESS_VALIDATION_FAILED"


def _payload_hash(payload: Any) -> str:
    if hasattr(payload, "model_dump"):
        value = payload.model_dump(mode="json")
    else:
        value = payload
    # requested_at is transport metadata rather than mutation identity.
    # A client retry may legitimately rebuild it while retaining request_id;
    # every business field remains part of the conflict hash.
    if isinstance(value, dict):
        value = {
            key: item
            for key, item in value.items()
            if key != "requested_at"
        }
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
