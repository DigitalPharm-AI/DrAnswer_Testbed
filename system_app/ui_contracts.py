from __future__ import annotations

from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from shared.chat_contracts import (
    ChatMessageContent,
    ChatMessageType,
    RequestedReturnType,
    require_structured_response_source,
)
from shared.contract_boundary import remove_retired_conversation_fields
from shared.public_ids import (
    AssistantMessageId,
    DoseEventId,
    RequestId,
    UserMessageId,
)


class StrictUiContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


UiDataT = TypeVar("UiDataT")


class UiSuccessResponse(StrictUiContract, Generic[UiDataT]):
    success: Literal[True]
    data: UiDataT
    error: None


class UiError(StrictUiContract):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None

    @field_validator("details")
    @classmethod
    def remove_retired_identifiers(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return remove_retired_conversation_fields(value)


class UiErrorResponse(StrictUiContract):
    success: Literal[False]
    data: None
    error: UiError


class ApplyMedicationScenarioRequest(StrictUiContract):
    request_id: RequestId
    scenario_id: str = Field(min_length=1, max_length=80)
    schedule_date: date

    @field_validator("scenario_id")
    @classmethod
    def normalize_required_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("blank_identifier_not_allowed")
        return normalized


class AdvanceClockRequest(StrictUiContract):
    request_id: RequestId
    minutes: Literal[30, 180]


class PlayClockRequest(StrictUiContract):
    speed_multiplier: Literal[1, 5, 15, 30, 60]


class EmptyUiRequest(StrictUiContract):
    pass


class ResetTestbedRequest(StrictUiContract):
    request_id: RequestId
    confirm: Literal[True]


class UpdateUiPolicyRequest(StrictUiContract):
    enabled: bool


class UiChatRequest(StrictUiContract):
    message: str = Field(min_length=1, max_length=2_000)
    requested_return_type: RequestedReturnType
    request_id: RequestId | None = None
    source_message_id: AssistantMessageId | None = None

    @field_validator("message")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("blank_message_not_allowed")
        return normalized

    @model_validator(mode="after")
    def validate_structured_response_source(self) -> UiChatRequest:
        require_structured_response_source(
            self.requested_return_type,
            self.source_message_id,
        )
        return self


UiPolicyKey = Literal[
    "medication_schedule_alert",
    "missed_dose_conversation",
]


class UiSimulationClock(StrictUiContract):
    current_time: datetime
    is_running: bool
    speed_multiplier: Literal[0, 1, 5, 15, 30, 60]


class UiMedicationDose(StrictUiContract):
    dose_event_id: DoseEventId
    medication_name: str
    treatment_area: str
    slot_label: str
    scheduled_for: datetime
    status: Literal["scheduled", "taken", "missed"]
    taken_at: datetime | None


class UiActiveScenario(StrictUiContract):
    scenario_id: str
    name: str
    schedule_date: date


class UiNutritionMetric(StrictUiContract):
    name: str
    intake: float
    threshold: float
    unit: str
    remaining: float
    percent: float
    bar_percent: float
    daily_exceeded: bool
    meal_exceeded: bool
    is_warning: bool
    basis_label: str
    risk_rank: int


class UiNutritionNutrientValue(StrictUiContract):
    value: float
    unit: str


class UiNutritionMealFood(StrictUiContract):
    food_ref_id: str
    food_name: str
    portion: str
    nutrients: dict[str, UiNutritionNutrientValue]


class UiNutritionMeal(StrictUiContract):
    meal_type: str
    meal_label: str
    meal_date: date
    meal_time: str
    scenario_key: str | None
    description: str
    foods: list[UiNutritionMealFood]


class UiNutritionScenario(StrictUiContract):
    key: str
    label: str
    description: str
    result_label: str
    is_recorded: bool


class UiNutritionDashboard(StrictUiContract):
    profile: dict[str, Any]
    summary: dict[str, Any]
    preferences: dict[str, Any]
    metrics: list[UiNutritionMetric]
    meals: list[UiNutritionMeal]
    scenarios: list[UiNutritionScenario]


class UiPolicies(StrictUiContract):
    medication_schedule_alert: bool
    missed_dose_conversation: bool


class UiBackendServerStatus(StrictUiContract):
    status: Literal[
        "ready",
        "failed",
        "timeout",
        "incompatible",
        "not_ready",
        "degraded",
    ]
    checked_at: datetime
    evidence: list[str]


class UiAiServerStatus(StrictUiContract):
    status: Literal[
        "ready",
        "not_ready",
        "failed",
        "timeout",
        "unreachable",
        "incompatible",
    ]
    checked_at: datetime
    evidence: list[str]


class UiNotificationMetadata(StrictUiContract):
    severity: Literal["info", "reminder", "warning", "critical"]


class UiNotificationInteraction(StrictUiContract):
    kind: Literal["open_chat"]
    state: Literal["pending", "ready", "failed"]
    message_id: AssistantMessageId | None


class UiNotification(StrictUiContract):
    id: str = Field(pattern=r"^notif_[0-9a-f]{32}$")
    notification_type: str
    title: str
    body: str
    visible_at: datetime
    visible_at_label: str
    acknowledged: bool
    metadata: UiNotificationMetadata
    interaction: UiNotificationInteraction | None
    dose_status: str | None
    related_dose_event_id: DoseEventId | None


class UiDashboardData(StrictUiContract):
    clock: UiSimulationClock
    simulation_ready: bool
    active_scenario: UiActiveScenario | None
    medications: list[UiMedicationDose]
    nutrition: UiNutritionDashboard
    policies: UiPolicies
    notifications: list[UiNotification]


class UiMedicationScenarioItem(StrictUiContract):
    medication_id: str
    medication_name: str
    dosage: str
    treatment_area: str
    slot_label: str
    scheduled_time: str


class UiMedicationScenario(StrictUiContract):
    scenario_id: str
    name: str
    medications: list[UiMedicationScenarioItem]


class UiMedicationScenarioListData(StrictUiContract):
    scenarios: list[UiMedicationScenario]


class UiMedicationScenarioApplyData(StrictUiContract):
    scenario: UiActiveScenario
    medications: list[UiMedicationDose]


class UiClockData(StrictUiContract):
    clock: UiSimulationClock


class UiDoseData(StrictUiContract):
    dose: UiMedicationDose


class UiNutritionData(UiNutritionDashboard):
    pass


class UiPoliciesData(StrictUiContract):
    policies: UiPolicies


class UiSystemStatusData(StrictUiContract):
    backend_server: UiBackendServerStatus
    ai_server: UiAiServerStatus


class UiTestbedResetData(StrictUiContract):
    request_id: RequestId
    reset_applied: Literal[True]
    reset_at: datetime


class UiNotificationListData(StrictUiContract):
    notifications: list[UiNotification]
    last_seen_id: str | None = Field(
        pattern=r"^notif_[0-9a-f]{32}$",
    )
    current_time: datetime


class UiNotificationDetailData(StrictUiContract):
    notification: UiNotification


class UiNotificationAcknowledgementData(StrictUiContract):
    acknowledged_count: int


class UiChatSyncData(StrictUiContract):
    request_id: RequestId
    user_message_id: UserMessageId
    user_sort_sequence: int = Field(ge=1)
    assistant_message_id: AssistantMessageId
    assistant_sort_sequence: int = Field(ge=1)
    message_type: ChatMessageType
    message: ChatMessageContent
    message_at: datetime
    display_message_at: datetime


class UiChatHistoryMessage(StrictUiContract):
    message_id: UserMessageId | AssistantMessageId
    sort_sequence: int = Field(ge=1)
    role: str
    message_type: ChatMessageType | None
    message: str | None
    content: ChatMessageContent | None
    created_at: datetime
    processing_status: str
    response_message_id: UserMessageId | None
    source_message_id: AssistantMessageId | None
    reaction: Literal["like", "dislike"] | None
    opinion_submitted: bool
    opinion_submitted_at: datetime | None


class UiChatHistoryDay(StrictUiContract):
    date: date
    messages: list[UiChatHistoryMessage]


class UiChatHistoryData(StrictUiContract):
    days: list[UiChatHistoryDay]
    next_before_date: date | None


class UiDashboardResponse(UiSuccessResponse[UiDashboardData]):
    pass


class UiMedicationScenarioListResponse(
    UiSuccessResponse[UiMedicationScenarioListData]
):
    pass


class UiMedicationScenarioApplyResponse(
    UiSuccessResponse[UiMedicationScenarioApplyData]
):
    pass


class UiClockResponse(UiSuccessResponse[UiClockData]):
    pass


class UiDoseResponse(UiSuccessResponse[UiDoseData]):
    pass


class UiNutritionResponse(UiSuccessResponse[UiNutritionData]):
    pass


class UiPoliciesResponse(UiSuccessResponse[UiPoliciesData]):
    pass


class UiSystemStatusResponse(UiSuccessResponse[UiSystemStatusData]):
    pass


class UiTestbedResetResponse(UiSuccessResponse[UiTestbedResetData]):
    pass


class UiNotificationListResponse(
    UiSuccessResponse[UiNotificationListData]
):
    pass


class UiNotificationDetailResponse(
    UiSuccessResponse[UiNotificationDetailData]
):
    pass


class UiNotificationAcknowledgementResponse(
    UiSuccessResponse[UiNotificationAcknowledgementData]
):
    pass


class UiChatHistoryResponse(UiSuccessResponse[UiChatHistoryData]):
    pass


class UiChatSyncResponse(UiSuccessResponse[UiChatSyncData]):
    pass
