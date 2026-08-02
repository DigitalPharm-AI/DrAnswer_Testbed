from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.contract_boundary import remove_retired_conversation_fields
from shared.public_ids import (
    DoseEventId,
    NotificationPolicyId,
    PatientId,
    RequestId,
    UserMessageId,
    require_public_id,
)
from shared.time_utils import (
    require_aware_datetime,
    require_optional_aware_datetime,
)

RecordResourceType = Literal[
    "nutrition_meal",
    "nutrition_food",
    "medication_dose_event",
    "medication_side_effect",
]
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


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NutritionNutrients(StrictContractModel):
    calories: float | None = None
    protein: float | None = None
    sodium: float | None = None
    fat: float | None = None
    carbohydrates: float | None = None


class NutritionFoodItem(StrictContractModel):
    food_ref_id: str | None = None
    food_name: str = Field(min_length=1)
    portion: str | None = None
    nutrients: NutritionNutrients | None = None


class NutritionMealMutationPayload(StrictContractModel):
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    meal_date: date | None = None
    meal_time: str | None = Field(
        default=None,
        pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$",
        description="24-hour HH:mm.",
    )
    scenario_key: str | None = None
    description: str | None = None
    foods: list[NutritionFoodItem] | None = None
    reason: str | None = None

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
    nutrients: NutritionNutrients | None = None

    @model_validator(mode="after")
    def require_change(self) -> NutritionFoodMutationPayload:
        mutable_values = (self.food_ref_id, self.food_name, self.portion, self.nutrients)
        if not any(value is not None for value in mutable_values):
            raise ValueError("nutrition_food_payload_empty")
        return self


class MedicationDoseEventMutationPayload(StrictContractModel):
    status: Literal["taken"]
    taken_at: datetime | None = None
    reason: str | None = None

    @field_validator("taken_at")
    @classmethod
    def validate_taken_at(cls, value: datetime | None) -> datetime | None:
        return require_optional_aware_datetime(value)


class ProCtcaeQuestion(StrictContractModel):
    item_code: str = Field(min_length=1)
    question: str = Field(min_length=1)
    response_type: str = Field(min_length=1)
    response_options: list[str] = Field(min_length=1)

    @field_validator("response_options")
    @classmethod
    def validate_response_options(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("pro_ctcae_response_option_must_not_be_blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("pro_ctcae_response_options_must_be_unique")
        return normalized


class ProCtcaeResponse(StrictContractModel):
    item_code: str = Field(min_length=1)
    response_index: int = Field(ge=0)
    response_text: str = Field(min_length=1)


class ProCtcaeSeverityResult(StrictContractModel):
    questions: list[ProCtcaeQuestion] = Field(min_length=1)
    responses: list[ProCtcaeResponse] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_question_responses(self) -> ProCtcaeSeverityResult:
        questions_by_code = {
            question.item_code: question for question in self.questions
        }
        if len(questions_by_code) != len(self.questions):
            raise ValueError("pro_ctcae_question_item_codes_must_be_unique")
        responses_by_code = {
            response.item_code: response for response in self.responses
        }
        if len(responses_by_code) != len(self.responses):
            raise ValueError("pro_ctcae_response_item_codes_must_be_unique")
        if set(responses_by_code) != set(questions_by_code):
            raise ValueError("pro_ctcae_response_required_for_each_question")
        for item_code, response in responses_by_code.items():
            options = questions_by_code[item_code].response_options
            if response.response_index >= len(options):
                raise ValueError("pro_ctcae_response_index_out_of_range")
            if response.response_text.strip() != options[response.response_index]:
                raise ValueError("pro_ctcae_response_text_mismatch")
        return self


class MedicationSideEffectMutationPayload(StrictContractModel):
    medication_name: str | None = None
    symptom_text: str = Field(min_length=1)
    symptom_onset_text: str | None = None
    suspected: bool
    severity: ProCtcaeSeverityResult
    matched_effects: list[str] | None = None
    matched_items: list[str] | None = None
    related_dose_event_id: DoseEventId | None = Field(
        default=None,
        description="Related medication dose-event public opaque ID.",
    )


RecordPayload = (
    NutritionMealMutationPayload
    | NutritionFoodMutationPayload
    | MedicationDoseEventMutationPayload
    | MedicationSideEffectMutationPayload
)


class RecordChangeRequest(StrictContractModel):
    request_id: RequestId
    source_chat_request_id: RequestId
    confirmation_message_id: UserMessageId = Field(
        description="Confirmed user chat_messages.public_id (opaque string).",
    )
    patient_id: PatientId
    resource_type: RecordResourceType
    operation: RecordOperation
    record_id: str | None = Field(
        description="Target resource public opaque ID.",
    )
    parent_record_id: str | None = Field(
        description="Parent resource public opaque ID when required.",
    )
    expected_version: int | None = Field(ge=1)
    payload: RecordPayload | None
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)

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
            if self.record_id is not None:
                require_public_id(self.record_id, "meal")
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
            if self.record_id is not None:
                require_public_id(self.record_id, "food")
            if self.operation == "create":
                raise ValueError("nutrition_food_create_not_supported")
            if not self.parent_record_id:
                raise ValueError("nutrition_food_parent_record_id_required")
            require_public_id(self.parent_record_id, "meal")
            if self.operation != "delete" and not isinstance(self.payload, NutritionFoodMutationPayload):
                raise ValueError("nutrition_food_payload_required")

        elif self.resource_type == "medication_dose_event":
            if self.record_id is not None:
                require_public_id(self.record_id, "dose_event")
            if self.parent_record_id is not None:
                raise ValueError("medication_parent_record_id_must_be_null")
            if self.operation != "update":
                raise ValueError("medication_dose_event_only_supports_update")
            if not isinstance(self.payload, MedicationDoseEventMutationPayload):
                raise ValueError("medication_dose_event_payload_required")

        elif self.resource_type == "medication_side_effect":
            if self.record_id is not None:
                require_public_id(self.record_id, "side_effect")
            if self.parent_record_id is not None:
                raise ValueError(
                    "medication_side_effect_parent_record_id_must_be_null"
                )
            if self.operation != "create":
                raise ValueError("medication_side_effect_only_supports_create")
            if not isinstance(self.payload, MedicationSideEffectMutationPayload):
                raise ValueError("medication_side_effect_payload_required")

        return self


class ContractError(StrictContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] | None

    @field_validator("details")
    @classmethod
    def remove_retired_identifiers(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return remove_retired_conversation_fields(value)


class CommonErrorResponse(StrictContractModel):
    request_id: RequestId | None
    error: ContractError


class RecordChangeResult(StrictContractModel):
    resource_type: RecordResourceType
    operation: RecordOperation
    record_id: str = Field(
        min_length=1,
        description="Target resource public opaque ID.",
    )
    parent_record_id: str | None = Field(
        default=None,
        description="Parent resource public opaque ID when required.",
    )
    version: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_public_ids(self) -> RecordChangeResult:
        kind_by_resource = {
            "nutrition_meal": "meal",
            "nutrition_food": "food",
            "medication_dose_event": "dose_event",
            "medication_side_effect": "side_effect",
        }
        require_public_id(
            self.record_id,
            kind_by_resource[self.resource_type],
        )
        if self.resource_type == "nutrition_food":
            if self.parent_record_id is None:
                raise ValueError(
                    "nutrition_food_parent_record_id_required"
                )
            require_public_id(self.parent_record_id, "meal")
        elif self.parent_record_id is not None:
            raise ValueError("parent_record_id_must_be_null")
        return self


class RecordChangeResponse(StrictContractModel):
    request_id: RequestId
    result: RecordChangeResult
    processed_at: datetime

    @field_validator("processed_at")
    @classmethod
    def validate_processed_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)


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
    request_id: RequestId
    source_chat_request_id: RequestId
    confirmation_message_id: UserMessageId = Field(
        description="Confirmed user chat_messages.public_id (opaque string).",
    )
    patient_id: PatientId
    policy_id: NotificationPolicyId
    expected_version: int = Field(ge=1)
    payload: NotificationPolicyDecisionPayload
    requested_at: datetime

    @field_validator("requested_at")
    @classmethod
    def validate_requested_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)


class NotificationPolicyChangeResult(StrictContractModel):
    policy_id: NotificationPolicyId
    decision: PolicyDecision
    version: int = Field(ge=1)


class NotificationPolicyChangeResponse(StrictContractModel):
    request_id: RequestId
    result: NotificationPolicyChangeResult
    processed_at: datetime

    @field_validator("processed_at")
    @classmethod
    def validate_processed_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)
