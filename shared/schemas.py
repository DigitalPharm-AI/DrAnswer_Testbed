from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatTurn(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str
    created_at: datetime | None = None


class AgentCallbackContext(BaseModel):
    app_base_url: str
    notification_id: int | None = None
    job_id: int | None = None
    conversation_id: str | None = None


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
    dose_event_id: int | None = None
    medication_name: str
    slot_label: str
    scheduled_for: datetime
    taken_at: datetime | None = None
    status: Literal["scheduled", "taken", "missed"]


class MissedDoseReplyContext(BaseModel):
    dose_event_id: int | None = None
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
    phr_patient_key: str | None = None
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
    notes: str | None = None
    callback_context: AgentCallbackContext | None = None


class MissedDoseEventPayload(BaseModel):
    patient_id: str
    phr_patient_key: str | None = None
    dose_event_id: int
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


class MissedDoseAssessment(BaseModel):
    dose_event_id: int
    likely_reason: str
    side_effect_signal: bool
    symptom_summary: str
    follow_up_questions: list[str]
    recommendation: str


class MultiturnChatRequest(BaseModel):
    patient_id: str
    phr_patient_key: str | None = None
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


class AgentAsyncAccepted(BaseModel):
    request_id: str
    task_type: str
    status: Literal["accepted", "duplicate"]
    callback_expected: bool = True
    message: str = ""


class AgentAsyncTaskActionRequest(BaseModel):
    action: Literal["retry", "dismiss"]
    reason: str = ""


class AgentAsyncJobResultRequest(BaseModel):
    request_id: str
    task_type: Literal["daily_pattern", "missed_dose"]
    response: AgentResponse
    job_id: int | None = None
    related_dose_event_id: int | None = None
    idempotency_key: str | None = None


class AgentAsyncChatResultRequest(BaseModel):
    request_id: str
    event_type: str = "multiturn_chat"
    message: str = ""
    notification_id: int | None = None
    response: AgentResponse
    idempotency_key: str | None = None


class AgentAsyncPolicyChangeRequest(BaseModel):
    request_id: str
    source_event_type: str = "multiturn_chat"
    notification_id: int | None = None
    response: AgentResponse
    idempotency_key: str | None = None


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


class AgentAsyncFailureRequest(BaseModel):
    request_id: str
    task_type: str
    message: str
    error_type: str = "agent_async_task_failed"
    trace_id: str | None = None
    agent_name: str | None = None
    decision_type: str | None = None
    job_id: int | None = None
    notification_id: int | None = None
    related_dose_event_id: int | None = None
    idempotency_key: str | None = None


class ToolCallResult(BaseModel):
    tool_name: str
    status: Literal["success", "error", "skipped"]
    response: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    idempotency_key: str | None = None


class SideEffectAssessmentRequest(BaseModel):
    phr_patient_key: str
    medication_name: str | None = None
    symptom_text: str
    recent_chat: list[ChatTurn] = Field(default_factory=list)
    dose_event_id: int | None = None


class SideEffectAssessmentResult(BaseModel):
    suspected: bool
    matched_effects: list[str] = Field(default_factory=list)
    matched_items: list[str] = Field(default_factory=list)
    severity: Literal["none", "low", "moderate", "high"]
    evidence: str
    recommendation: str


class AEProCtcaeAssessmentRequest(BaseModel):
    symptom_text: str
    symptom_normalize: str = ""
    threshold: float | None = None


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
    match_type: Literal["exact", "similarity", "other_symptoms"]
    matched_symptom_term: str = ""
    matched_korean_symptom_name: str = ""
    similarity: float = 0.0
    threshold: float
    scoring_method: str = "local_similarity"
    embedding_provider: str = ""
    sheet_name: str
    questions: list[AEProCtcaeQuestion] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)


class DoseTakenToolRequest(BaseModel):
    dose_event_id: int = Field(ge=1)
    taken_at: datetime | None = None
    reason: str = ""
    source_trace_id: str | None = None
    source_event_type: str = "agent_tool"


class DoseTakenToolResult(BaseModel):
    dose_event_id: int
    status: Literal["taken", "not_found"]
    taken_at: datetime | None = None
    message: str


class NutritionFoodPayload(BaseModel):
    food_ref_id: str = ""
    food_name: str
    portion: str = "1인분"
    nutrients: dict[str, Any] = Field(default_factory=dict)


class NutritionFoodSearchRequest(BaseModel):
    query: str
    patient_id: str | None = None
    limit: int = Field(default=10, ge=1, le=20)


class NutritionMealRecordRequest(BaseModel):
    patient_id: str | None = None
    foods: list[NutritionFoodPayload]
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"]
    meal_date: date | None = None
    meal_time: str | None = None
    scenario_key: str = ""
    description: str = ""


class NutritionMealUpdateRequest(BaseModel):
    patient_id: str | None = None
    foods: list[NutritionFoodPayload] | None = None
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] | None = None
    meal_date: date | None = None
    meal_time: str | None = None
    scenario_key: str | None = None
    description: str | None = None
    reason: str = ""


class NutritionMealDeleteRequest(BaseModel):
    patient_id: str | None = None
    reason: str = ""


class NutritionFoodUpdateRequest(BaseModel):
    patient_id: str | None = None
    food_ref_id: str | None = None
    food_name: str | None = None
    portion: str | None = None
    nutrients: dict[str, Any] | None = None
    reason: str = ""


class NutritionFoodDeleteRequest(BaseModel):
    patient_id: str | None = None
    reason: str = ""
    delete_empty_meal: bool = True


class NutritionMealListResult(BaseModel):
    success: bool = True
    meals: list[dict[str, Any]] = Field(default_factory=list)
    total: int = 0


class NutritionMealRecordResult(BaseModel):
    success: bool = True
    meal: dict[str, Any]
    daily_summary: dict[str, Any]
    alert_created: bool = False
    alert_id: int | None = None


class NutritionMealUpdateResult(BaseModel):
    success: bool = True
    meal: dict[str, Any]
    daily_summary: dict[str, Any]
    alert_created: bool = False
    alert_id: int | None = None
    updated: bool = True
    reason: str = ""


class NutritionMealDeleteResult(BaseModel):
    success: bool = True
    deleted_meal: dict[str, Any]
    daily_summary: dict[str, Any]
    reason: str = ""


class NutritionFoodUpdateResult(BaseModel):
    success: bool = True
    meal: dict[str, Any]
    food: dict[str, Any]
    daily_summary: dict[str, Any]
    updated: bool = True
    reason: str = ""


class NutritionFoodDeleteResult(BaseModel):
    success: bool = True
    meal_id: int
    food_id: int
    deleted_food: dict[str, Any]
    meal: dict[str, Any] | None = None
    daily_summary: dict[str, Any]
    meal_deleted: bool = False
    reason: str = ""


class NutritionDailySummaryResult(BaseModel):
    success: bool = True
    daily_summary: dict[str, Any]


class NutritionFoodSearchResult(BaseModel):
    success: bool = True
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "sample"
    error: str = ""
    query: str = ""


class NutritionPreferenceFactRequest(BaseModel):
    patient_id: str | None = None
    predicate: Literal[
        "likes",
        "dislikes",
        "prefers",
        "avoids_by_preference",
        "allergic_to",
        "medically_avoids",
        "religious_avoids",
    ]
    object_label: str
    object_type: Literal["food", "ingredient", "food_category", "cuisine", "preparation", "nutrient", "nutrient_risk", "restriction", "diet_style"] = "food"
    strength: float = Field(default=1.0, ge=0.0, le=1.0)
    safety_level: Literal["hard", "soft"] | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "agent_tool"
    evidence_text: str = ""
    source_trace_id: str = ""


class NutritionPreferenceFactResult(BaseModel):
    success: bool = True
    fact: dict[str, Any]
    preferences: dict[str, Any]


class NutritionPreferenceSummaryResult(BaseModel):
    success: bool = True
    preferences: dict[str, Any]


class PhrMedicationRegistrationItem(BaseModel):
    item_name: str
    dosage: str = ""


class PhrPatientRegistrationRequest(BaseModel):
    medications: list[PhrMedicationRegistrationItem] = Field(default_factory=list)


class PhrRegisteredMedication(BaseModel):
    item_name: str
    dosage: str = ""
    active: bool = True
    precautions_text: str = ""


class PhrPatientRegistrationResult(BaseModel):
    phr_patient_key: str
    medications: list[PhrRegisteredMedication] = Field(default_factory=list)


class PolicyApplyItemResult(BaseModel):
    slot_label: str
    applied: bool
    message: str


class PolicyApplyRequest(BaseModel):
    policies: list[NotificationPolicyDelta] = Field(default_factory=list)
    policy: NotificationPolicyDelta | None = None
    idempotency_key: str
    source_trace_id: str | None = None
    source_event_type: str = "agent_tool"


class PolicyApplyResult(BaseModel):
    idempotency_key: str
    results: list[PolicyApplyItemResult]
    all_applied: bool


class SystemPolicyApplyItemResult(BaseModel):
    policy_key: str
    value: str = ""
    applied: bool
    message: str


class SystemPolicyApplyRequest(BaseModel):
    policies: list[SystemPolicyDelta] = Field(default_factory=list)
    policy: SystemPolicyDelta | None = None
    idempotency_key: str
    source_trace_id: str | None = None
    source_event_type: str = "agent_tool"


class SystemPolicyApplyResult(BaseModel):
    idempotency_key: str
    results: list[SystemPolicyApplyItemResult]
    all_applied: bool


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


class NutritionRecommendRequest(BaseModel):
    patient_id: str | None = None
    constraints: dict[str, str] = Field(default_factory=dict)
    meal_type: str | None = None
    limit: int = Field(default=5, ge=1, le=20)
    randomize: bool = True


class NutritionRecommendResult(BaseModel):
    success: bool = True
    disease: str = ""
    ckd_risk: str = ""
    constraints_applied: dict[str, str] = Field(default_factory=dict)
    limits_used: dict[str, Any] = Field(default_factory=dict)
    recommendations: list[dict[str, Any]] = Field(default_factory=list)
    blocked_count: int = 0
    total_candidates: int = 0
    randomized: bool = False
    meal_type_requested: str = ""
    meal_candidate_count: int = 0
    non_meal_candidate_count: int = 0
    error: str = ""
