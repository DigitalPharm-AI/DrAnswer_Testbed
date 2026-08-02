from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.backend_v13_contracts import ProCtcaeSeverityResult
from shared.contract_boundary import remove_retired_conversation_fields


class ChatTurn(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime | None = None


class AgentCallbackContext(BaseModel):
    app_base_url: str
    notification_id: int | None = None
    job_id: int | None = None


class MultipleChoiceOption(BaseModel):
    number: int = Field(ge=1)
    value: str
    label: str
    text: str
    description: str = ""


class MultipleChoicePrompt(BaseModel):
    prompt_type: str
    question: str
    options: list[MultipleChoiceOption]
    recommended_value: str | None = None


class AgentModelTierRequest(BaseModel):
    model_tier: Literal["fast", "sonnet"]


class AgentModelConfig(BaseModel):
    provider: str
    model_tier: Literal["fast", "sonnet"]
    model_id: str
    available_tiers: dict[str, str]


class SlotAdherenceSummary(BaseModel):
    slot_label: str
    scheduled_count: int
    taken_count: int
    missed_count: int
    miss_rate: float


class DosePatternEvent(BaseModel):
    dose_event_id: str | int | None = None
    medication_name: str
    slot_label: str
    scheduled_for: datetime
    taken_at: datetime | None = None
    status: Literal["scheduled", "taken", "missed"]


class MissedDoseReplyContext(BaseModel):
    dose_event_id: str | int | None = None
    medication_name: str = ""
    slot_label: str = ""
    scheduled_for: datetime | None = None
    patient_reply: str
    agent_reply: str = ""
    status: str = ""
    reply_understanding: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class ResolvedNotificationPolicy(BaseModel):
    policy_key: str
    patient_id: str | None = None
    slot_label: str
    extra_reminders: int = Field(ge=0)
    interval_minutes: int = Field(ge=1)
    missed_dose_after_minutes: int = Field(ge=1)
    primary_reminder_timing: Literal["before", "at", "after"] = "at"
    primary_reminder_offset_minutes: int = Field(default=0, ge=0)
    medication_title_template: str
    medication_body_template: str
    extra_title_template: str
    extra_body_template: str
    missed_dose_title_template: str
    missed_dose_body_template: str
    source: Literal["excel_default", "patient_override"]
    reason: str = ""
    priority: int = 0


class ResolvedPolicyBoundary(BaseModel):
    boundary_key: str
    policy_key: str
    slot_label: str
    min_extra_reminders: int = Field(ge=0)
    max_extra_reminders: int = Field(ge=0)
    min_interval_minutes: int = Field(ge=1)
    max_interval_minutes: int = Field(ge=1)
    min_missed_dose_after_minutes: int = Field(ge=1)
    max_missed_dose_after_minutes: int = Field(ge=1)
    allowed_primary_reminder_timings: list[Literal["before", "at", "after"]]
    min_primary_reminder_offset_minutes: int = Field(ge=0)
    max_primary_reminder_offset_minutes: int = Field(ge=0)
    source: Literal["excel_boundary"] = "excel_boundary"
    reason: str = ""
    priority: int = 0


class PolicyWorkbookLoadResult(BaseModel):
    path: str
    loaded_count: int
    boundary_loaded_count: int = 0
    system_policy_loaded_count: int = 0
    errors: list[str] = Field(default_factory=list)


class DailyMedicationPattern(BaseModel):
    patient_id: str
    date: date
    window_start_date: date | None = None
    window_end_date: date | None = None
    window_days: int = Field(default=1, ge=1)
    observed_day_count: int = Field(default=1, ge=0)
    schedule_slots: list[str]
    dose_events: list[DosePatternEvent]
    slot_summaries: list[SlotAdherenceSummary]
    policy_context: list[ResolvedNotificationPolicy] = Field(default_factory=list)
    policy_boundaries: list[ResolvedPolicyBoundary] = Field(default_factory=list)
    missed_dose_reply_context: list[MissedDoseReplyContext] = Field(default_factory=list)
    conversation_context: list[ChatTurn] = Field(default_factory=list)
    # These snapshots come from the AI Server's fixed read-only Backend DB
    # queries. The public daily-pattern trigger remains disabled until the
    # v1.3 makes Backend the owner of the public daily-pattern trigger.
    active_schedule_snapshot: list[dict[str, Any]] = Field(
        default_factory=list,
    )
    current_policy_snapshot: list[dict[str, Any]] = Field(
        default_factory=list,
    )
    notes: str | None = None
    callback_context: AgentCallbackContext | None = None


class MissedDoseEventPayload(BaseModel):
    patient_id: str
    dose_event_id: str | int
    medication_name: str
    slot_label: str
    scheduled_for: datetime
    detected_at: datetime
    policy_context: ResolvedNotificationPolicy | None = None
    policy_boundary: ResolvedPolicyBoundary | None = None
    recent_slot_summaries: list[SlotAdherenceSummary] = Field(default_factory=list)
    adherence_pattern_context: dict[str, Any] = Field(default_factory=dict)
    tone_policy_context: dict[str, Any] = Field(default_factory=dict)
    chat_context: list[ChatTurn] = Field(default_factory=list)
    missed_dose_reply_context: list[MissedDoseReplyContext] = Field(default_factory=list)
    conversation_context: list[ChatTurn] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    callback_context: AgentCallbackContext | None = None


class NotificationPolicyDelta(BaseModel):
    policy_key: str | None = None
    slot_label: str
    extra_reminders: int = Field(ge=0)
    interval_minutes: int = Field(ge=1)
    missed_dose_after_minutes: int | None = Field(default=None, ge=1)
    primary_reminder_timing: Literal["before", "at", "after"] | None = None
    primary_reminder_offset_minutes: int | None = Field(default=None, ge=0)
    medication_title_template: str | None = None
    medication_body_template: str | None = None
    extra_title_template: str | None = None
    extra_body_template: str | None = None
    missed_dose_title_template: str | None = None
    missed_dose_body_template: str | None = None
    effective_start_date: date
    effective_end_date: date
    reason: str
    source: Literal["pattern_analysis", "patient_request", "system_request"]


class SystemPolicyDelta(BaseModel):
    policy_key: str
    value: str
    reason: str
    source: Literal["patient_request", "system_request"] = "patient_request"


class MultiturnChatRequest(BaseModel):
    patient_id: str
    event_type: str
    message: str
    current_time: datetime
    context: dict[str, Any] = Field(default_factory=dict)
    callback_context: AgentCallbackContext | None = None


class AgentResponse(BaseModel):
    trace_id: str
    agent_name: str
    prompt_version_id: str
    decision_type: str
    structured_payload: dict[str, Any]
    human_summary: str
    requires_conversation_alert: bool = False
    validation_passed: bool = True
    validation_errors: list[str] = Field(default_factory=list)


class AgentInternalTaskAccepted(BaseModel):
    request_id: str
    task_type: str
    status: Literal["accepted", "duplicate"]
    accepted_at: datetime


class AgentAsyncTaskActionRequest(BaseModel):
    action: Literal["retry", "dismiss"]
    reason: str = ""


class AgentAsyncPushMessageRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    type: str = "conversation_alert"
    message: str
    send_time: datetime | None = Field(default=None, alias="sendTime")
    title: str = "AI 알림"
    request_id: str | None = None
    idempotency_key: str | None = None
    related_dose_event_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    chat_category: str | None = None

    @field_validator("metadata")
    @classmethod
    def remove_retired_metadata(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return remove_retired_conversation_fields(value)


class AgentAsyncClinicianAlertRequest(BaseModel):
    request_id: str | None = None
    idempotency_key: str | None = None
    title: str = "의료진 확인 스텁"
    message: str = "미복용 패턴을 의료진 확인 대상으로 기록했습니다."
    visible_at: datetime | None = None
    related_dose_event_id: int | None = None
    pattern_code: str | None = None
    pattern_label: str | None = None
    reason: str = ""
    priority: Literal["low", "normal", "high"] = "normal"
    metadata: dict[str, Any] = Field(default_factory=dict)
    streak_metrics: dict[str, Any] = Field(default_factory=dict)
    callback_context: AgentCallbackContext | None = None

    @field_validator("metadata")
    @classmethod
    def remove_retired_metadata(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return remove_retired_conversation_fields(value)


class ToolCallResult(BaseModel):
    tool_name: str
    status: Literal["success", "error", "skipped", "confirmation_required"]
    response: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    idempotency_key: str | None = None
    elapsed_ms: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=1, ge=1)
    retryable: bool = False

    @field_validator("response")
    @classmethod
    def remove_retired_response_fields(
        cls,
        value: dict[str, Any],
    ) -> dict[str, Any]:
        return remove_retired_conversation_fields(value)


class SideEffectAssessmentResult(BaseModel):
    suspected: bool
    matched_effects: list[str] = Field(default_factory=list)
    matched_items: list[str] = Field(default_factory=list)
    severity: Literal["none", "low", "moderate", "high"]
    evidence: str
    recommendation: str
    reference_source: str = ""
    reference_status: str = ""
    reference_matches: list[dict[str, Any]] = Field(default_factory=list)


class SideEffectRecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    patient_id: str | None = None
    medication_name: str | None = None
    symptom_text: str
    symptom_onset_text: str = ""
    suspected: bool
    severity: ProCtcaeSeverityResult
    matched_effects: list[str] = Field(default_factory=list)
    matched_items: list[str] = Field(default_factory=list)
    related_dose_event_id: str | int | None = None


class SideEffectRecordView(BaseModel):
    id: str | int
    patient_id: str
    medication_name: str = ""
    symptom_text: str = ""
    symptom_onset_text: str = ""
    suspected: bool
    severity: ProCtcaeSeverityResult | dict[str, Any]
    matched_effects: list[str] = Field(default_factory=list)
    matched_items: list[str] = Field(default_factory=list)
    related_dose_event_id: str | int | None = None
    version: int = 1
    created_at: datetime
    updated_at: datetime | None = None


class SideEffectHistoryResult(BaseModel):
    success: bool = True
    target_date: date | None = None
    start_date: date | None = None
    end_date: date | None = None
    records: list[SideEffectRecordView] = Field(default_factory=list)
    total: int = 0


class AEProCtcaeAssessmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symptom_text: str


class AEProCtcaeQuestion(BaseModel):
    symptom_term: str
    korean_symptom_name: str
    item_code: str
    question: str
    response_type: str = ""
    response_options: list[str] = Field(default_factory=list)
    pdf_page: int | None = None
    sheet_name: str


class AEProCtcaeAssessmentResult(BaseModel):
    input_symptom: str
    matched: bool
    match_type: Literal[
        "exact",
        "similarity",
        "vector_llm_verified",
        "other_symptoms",
    ]
    matched_symptom_term: str = ""
    matched_korean_symptom_name: str = ""
    similarity: float = 0.0
    threshold: float
    scoring_method: str = "local_similarity"
    embedding_provider: str = ""
    sheet_name: str
    questions: list[AEProCtcaeQuestion] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class MedicationDoseEventView(BaseModel):
    dose_event_id: str | int
    patient_id: str
    medication_name: str
    slot_label: str
    scheduled_for: datetime
    status: str
    taken_at: datetime | None = None
    note: str = ""


class MedicationDoseStatusResult(BaseModel):
    success: bool = True
    patient_id: str
    target_date: date | None = None
    start_date: date
    end_date: date
    dose_events: list[MedicationDoseEventView] = Field(default_factory=list)
    total: int = 0
    summary_by_date: list[dict[str, Any]] = Field(default_factory=list)
    totals_by_status: dict[str, int] = Field(default_factory=dict)


class AgentNotificationRequest(BaseModel):
    title: str
    body: str
    notification_type: str = "conversation_alert"
    notification_id: int | None = None
    related_dose_event_id: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    visible_at: datetime | None = None
    chat_category: str | None = None
    idempotency_key: str | None = None
