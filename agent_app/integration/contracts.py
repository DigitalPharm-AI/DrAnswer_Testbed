from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RecordResourceType = Literal["nutrition_meal", "nutrition_food", "medication_dose_event"]
RecordOperation = Literal["create", "update", "delete"]
PolicyDecision = Literal["apply", "keep"]

POLICY_MIN_EXTRA_REMINDERS = 0
POLICY_MAX_EXTRA_REMINDERS = 5
POLICY_MIN_INTERVAL_MINUTES = 5
POLICY_MAX_INTERVAL_MINUTES = 60
POLICY_MIN_MISSED_DOSE_AFTER_MINUTES = 15
POLICY_MAX_MISSED_DOSE_AFTER_MINUTES = 240
POLICY_MIN_PRIMARY_REMINDER_OFFSET_MINUTES = 0
POLICY_MAX_PRIMARY_REMINDER_OFFSET_MINUTES = 120
POLICY_MAX_EFFECTIVE_DAYS = 365


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NutritionFoodItem(StrictContractModel):
    food_ref_id: str = ""
    food_name: str = Field(min_length=1)
    portion: str = "1인분"
    nutrients: dict[str, Any] = Field(default_factory=dict)


class NutritionMealMutationPayload(StrictContractModel):
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    meal_date: date | None = None
    meal_time: str | None = None
    scenario_key: str | None = None
    description: str | None = None
    foods: list[NutritionFoodItem] | None = None
    reason: str = ""

    @model_validator(mode="after")
    def require_change(self) -> NutritionMealMutationPayload:
        mutable_values = (
            self.meal_type,
            self.meal_date,
            self.meal_time,
            self.scenario_key,
            self.description,
            self.foods,
        )
        if not any(value is not None for value in mutable_values):
            raise ValueError("nutrition_meal_payload_empty")
        return self


class NutritionFoodMutationPayload(StrictContractModel):
    food_ref_id: str | None = None
    food_name: str | None = None
    portion: str | None = None
    nutrients: dict[str, Any] | None = None
    reason: str = ""

    @model_validator(mode="after")
    def require_change(self) -> NutritionFoodMutationPayload:
        mutable_values = (self.food_ref_id, self.food_name, self.portion, self.nutrients)
        if not any(value is not None for value in mutable_values):
            raise ValueError("nutrition_food_payload_empty")
        return self


class MedicationDoseEventMutationPayload(StrictContractModel):
    status: Literal["taken"]
    taken_at: datetime | None = None
    reason: str = ""

    @field_validator("taken_at")
    @classmethod
    def validate_taken_at(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_datetime(value) if value is not None else None


RecordPayload = NutritionMealMutationPayload | NutritionFoodMutationPayload | MedicationDoseEventMutationPayload


class RecordChangeRequest(StrictContractModel):
    request_id: str = Field(min_length=1)
    source_chat_request_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    confirmation_message_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    resource_type: RecordResourceType
    operation: RecordOperation
    record_id: str | None = None
    parent_record_id: str | None = None
    expected_version: int | None = Field(default=None, ge=0)
    payload: RecordPayload | None = None
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_resource_contract(self) -> RecordChangeRequest:
        if self.operation == "create":
            if self.record_id is not None or self.expected_version is not None:
                raise ValueError("create_record_id_and_expected_version_must_be_null")
            if self.payload is None:
                raise ValueError("create_payload_required")
        else:
            if not self.record_id:
                raise ValueError("record_id_required")
            if self.expected_version is None:
                raise ValueError("expected_version_required")

        if self.operation == "delete":
            if self.payload is not None:
                raise ValueError("delete_payload_must_be_null")
        elif self.payload is None:
            raise ValueError("mutation_payload_required")

        if self.resource_type == "nutrition_meal":
            if self.parent_record_id is not None:
                raise ValueError("nutrition_meal_parent_record_id_must_be_null")
            if self.operation not in {"create", "update", "delete"}:
                raise ValueError("unsupported_nutrition_meal_operation")
            if self.operation != "delete" and not isinstance(self.payload, NutritionMealMutationPayload):
                raise ValueError("nutrition_meal_payload_required")
            if self.operation == "create":
                meal_payload = self.payload
                if not isinstance(meal_payload, NutritionMealMutationPayload):
                    raise ValueError("nutrition_meal_payload_required")
                if meal_payload.meal_type is None or not meal_payload.foods:
                    raise ValueError("nutrition_meal_create_requires_meal_type_and_foods")

        elif self.resource_type == "nutrition_food":
            if self.operation == "create":
                raise ValueError("nutrition_food_create_not_supported")
            if not self.parent_record_id:
                raise ValueError("nutrition_food_parent_record_id_required")
            if self.operation != "delete" and not isinstance(self.payload, NutritionFoodMutationPayload):
                raise ValueError("nutrition_food_payload_required")

        elif self.resource_type == "medication_dose_event":
            if self.parent_record_id is not None:
                raise ValueError("medication_parent_record_id_must_be_null")
            if self.operation != "update":
                raise ValueError("medication_dose_event_only_supports_update")
            if not isinstance(self.payload, MedicationDoseEventMutationPayload):
                raise ValueError("medication_dose_event_payload_required")

        return self


class ContractError(StrictContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] | None = None


class RecordChangeResult(StrictContractModel):
    resource_type: RecordResourceType
    operation: RecordOperation
    record_id: str = Field(min_length=1)
    parent_record_id: str | None = None
    version: int | None = Field(default=None, ge=0)


class RecordChangeResponse(StrictContractModel):
    success: bool
    request_id: str = Field(min_length=1)
    result: RecordChangeResult | None = None
    error: ContractError | None = None
    processed_at: datetime

    @field_validator("processed_at")
    @classmethod
    def validate_processed_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> RecordChangeResponse:
        if self.success and (self.result is None or self.error is not None):
            raise ValueError("successful_response_requires_result_without_error")
        if not self.success and (self.result is not None or self.error is None):
            raise ValueError("failed_response_requires_error_without_result")
        return self


class NotificationPolicyChanges(StrictContractModel):
    extra_reminders: int | None = Field(
        default=None,
        ge=POLICY_MIN_EXTRA_REMINDERS,
        le=POLICY_MAX_EXTRA_REMINDERS,
    )
    interval_minutes: int | None = Field(
        default=None,
        ge=POLICY_MIN_INTERVAL_MINUTES,
        le=POLICY_MAX_INTERVAL_MINUTES,
    )
    missed_dose_after_minutes: int | None = Field(
        default=None,
        ge=POLICY_MIN_MISSED_DOSE_AFTER_MINUTES,
        le=POLICY_MAX_MISSED_DOSE_AFTER_MINUTES,
    )
    primary_reminder_timing: Literal["before", "at", "after"] | None = None
    primary_reminder_offset_minutes: int | None = Field(
        default=None,
        ge=POLICY_MIN_PRIMARY_REMINDER_OFFSET_MINUTES,
        le=POLICY_MAX_PRIMARY_REMINDER_OFFSET_MINUTES,
    )
    effective_start_date: date | None = None
    effective_end_date: date | None = None

    @model_validator(mode="after")
    def validate_changes(self) -> NotificationPolicyChanges:
        changed_fields = self.model_fields_set
        if not changed_fields or not any(getattr(self, field_name) is not None for field_name in changed_fields):
            raise ValueError("policy_changes_empty")
        if (
            self.effective_start_date is not None
            and self.effective_end_date is not None
            and self.effective_start_date > self.effective_end_date
        ):
            raise ValueError("policy_effective_date_range_invalid")
        if (
            self.effective_start_date is not None
            and self.effective_end_date is not None
            and (self.effective_end_date - self.effective_start_date).days + 1 > POLICY_MAX_EFFECTIVE_DAYS
        ):
            raise ValueError("policy_effective_date_range_too_large")
        if self.primary_reminder_timing == "at" and self.primary_reminder_offset_minutes not in {None, 0}:
            raise ValueError("policy_at_timing_requires_zero_offset")
        return self


class NotificationPolicyDecisionPayload(StrictContractModel):
    decision: PolicyDecision
    changes: NotificationPolicyChanges | None = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_decision(self) -> NotificationPolicyDecisionPayload:
        if self.decision == "apply" and self.changes is None:
            raise ValueError("apply_requires_changes")
        if self.decision == "keep" and self.changes is not None:
            raise ValueError("keep_requires_null_changes")
        return self


class NotificationPolicyChangeRequest(StrictContractModel):
    request_id: str = Field(min_length=1)
    source_chat_request_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    confirmation_message_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    policy_id: str = Field(min_length=1)
    expected_version: int = Field(ge=0)
    payload: NotificationPolicyDecisionPayload
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)


class NotificationPolicyChangeResult(StrictContractModel):
    policy_id: str = Field(min_length=1)
    decision: PolicyDecision
    applied: bool
    version: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_application_state(self) -> NotificationPolicyChangeResult:
        if self.decision == "apply" and not self.applied:
            raise ValueError("apply_result_must_be_applied")
        if self.decision == "keep" and self.applied:
            raise ValueError("keep_result_must_not_be_applied")
        return self


class NotificationPolicyChangeResponse(StrictContractModel):
    success: bool
    request_id: str = Field(min_length=1)
    result: NotificationPolicyChangeResult | None = None
    error: ContractError | None = None
    processed_at: datetime

    @field_validator("processed_at")
    @classmethod
    def validate_processed_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_outcome(self) -> NotificationPolicyChangeResponse:
        if self.success and (self.result is None or self.error is not None):
            raise ValueError("successful_response_requires_result_without_error")
        if not self.success and (self.result is not None or self.error is None):
            raise ValueError("failed_response_requires_error_without_result")
        return self
