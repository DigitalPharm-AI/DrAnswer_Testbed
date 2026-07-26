from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_app.integration.contracts import (
    MedicationDoseEventMutationPayload,
    NotificationPolicyChangeRequest,
    NotificationPolicyChanges,
    NotificationPolicyDecisionPayload,
    NutritionFoodMutationPayload,
    NutritionMealMutationPayload,
    RecordChangeRequest,
)
from agent_app.tools.names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
)


class ConfirmedMutationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    source_chat_request_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    confirmation_message_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    requested_at: datetime


def record_change_request_from_tool(
    tool_name: str,
    arguments: dict[str, Any],
    context: ConfirmedMutationContext,
) -> RecordChangeRequest:
    values = dict(arguments)
    values.pop("patient_id", None)

    if tool_name == CREATE_NUTRITION_MEAL_RECORD:
        return _record_request(
            context,
            resource_type="nutrition_meal",
            operation="create",
            payload=NutritionMealMutationPayload.model_validate(values),
        )

    expected_version = _required_version(values)

    if tool_name == UPDATE_NUTRITION_MEAL_RECORD:
        record_id = _pop_required_id(values, "meal_id")
        return _record_request(
            context,
            resource_type="nutrition_meal",
            operation="update",
            record_id=record_id,
            expected_version=expected_version,
            payload=NutritionMealMutationPayload.model_validate(values),
        )

    if tool_name == DELETE_NUTRITION_MEAL_RECORD:
        record_id = _pop_required_id(values, "meal_id")
        return _record_request(
            context,
            resource_type="nutrition_meal",
            operation="delete",
            record_id=record_id,
            expected_version=expected_version,
        )

    if tool_name == UPDATE_NUTRITION_FOOD_RECORD:
        parent_record_id = _pop_required_id(values, "meal_id")
        record_id = _pop_required_id(values, "food_id")
        return _record_request(
            context,
            resource_type="nutrition_food",
            operation="update",
            record_id=record_id,
            parent_record_id=parent_record_id,
            expected_version=expected_version,
            payload=NutritionFoodMutationPayload.model_validate(values),
        )

    if tool_name == DELETE_NUTRITION_FOOD_RECORD:
        parent_record_id = _pop_required_id(values, "meal_id")
        record_id = _pop_required_id(values, "food_id")
        if "delete_empty_meal" in values:
            raise ValueError("delete_empty_meal_is_tool_managed")
        if values:
            raise ValueError(
                f"unsupported_nutrition_food_delete_arguments:{','.join(sorted(values))}"
            )
        return _record_request(
            context,
            resource_type="nutrition_food",
            operation="delete",
            record_id=record_id,
            parent_record_id=parent_record_id,
            expected_version=expected_version,
        )

    if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        record_id = _pop_required_id(values, "dose_event_id")
        values.pop("source_trace_id", None)
        values.pop("source_event_type", None)
        payload = MedicationDoseEventMutationPayload(
            status="taken",
            taken_at=context.requested_at,
            reason=str(values.pop("reason", "") or ""),
        )
        if values:
            raise ValueError(f"unsupported_medication_arguments:{','.join(sorted(values))}")
        return _record_request(
            context,
            resource_type="medication_dose_event",
            operation="update",
            record_id=record_id,
            expected_version=expected_version,
            payload=payload,
        )

    raise ValueError(f"unsupported_record_mutation_tool:{tool_name}")


def notification_policy_request(
    *,
    context: ConfirmedMutationContext,
    policy_id: str,
    expected_version: int,
    decision: str,
    changes: dict[str, Any] | None,
    reason: str,
) -> NotificationPolicyChangeRequest:
    return NotificationPolicyChangeRequest(
        request_id=context.request_id,
        source_chat_request_id=context.source_chat_request_id,
        conversation_id=context.conversation_id,
        confirmation_message_id=context.confirmation_message_id,
        patient_id=context.patient_id,
        policy_id=policy_id,
        expected_version=expected_version,
        payload=NotificationPolicyDecisionPayload(
            decision=decision,
            changes=NotificationPolicyChanges.model_validate(changes) if changes is not None else None,
            reason=reason,
        ),
        requested_at=context.requested_at,
    )


def _record_request(
    context: ConfirmedMutationContext,
    *,
    resource_type: str,
    operation: str,
    record_id: str | None = None,
    parent_record_id: str | None = None,
    expected_version: int | None = None,
    payload=None,
) -> RecordChangeRequest:
    return RecordChangeRequest(
        request_id=context.request_id,
        source_chat_request_id=context.source_chat_request_id,
        conversation_id=context.conversation_id,
        confirmation_message_id=context.confirmation_message_id,
        patient_id=context.patient_id,
        resource_type=resource_type,
        operation=operation,
        record_id=record_id,
        parent_record_id=parent_record_id,
        expected_version=expected_version,
        payload=payload,
        requested_at=context.requested_at,
    )


def _required_version(values: dict[str, Any]) -> int:
    raw = values.pop("expected_version", None)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError("expected_version_required")
    return raw


def _pop_required_id(values: dict[str, Any], name: str) -> str:
    raw = values.pop(name, None)
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        raise ValueError(f"{name}_required")
    return str(raw)
