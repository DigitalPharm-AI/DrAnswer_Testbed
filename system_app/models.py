from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.time_utils import utc_now


def utcnow() -> datetime:
    return utc_now()


class Base(DeclarativeBase):
    pass


class MedicationPlan(Base):
    __tablename__ = "medication_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    medication_name: Mapped[str] = mapped_column(String(255))
    dosage: Mapped[str] = mapped_column(String(255), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimulationPatientProfile(Base):
    __tablename__ = "simulation_patient_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    local_patient_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    phr_patient_key: Mapped[str] = mapped_column(String(160), default="")
    sync_status: Mapped[str] = mapped_column(String(40), default="unregistered")
    error_message: Mapped[str] = mapped_column(Text, default="")
    registered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NutritionProfile(Base):
    __tablename__ = "nutrition_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, default="demo-patient")
    name: Mapped[str] = mapped_column(String(120), default="데모 환자")
    age: Mapped[int] = mapped_column(Integer, default=55)
    gender: Mapped[str] = mapped_column(String(20), default="male")
    height: Mapped[float] = mapped_column(Float, default=170.0)
    weight: Mapped[float] = mapped_column(Float, default=70.0)
    disease: Mapped[str] = mapped_column(String(80), default="kidney_cancer")
    activity_level: Mapped[str] = mapped_column(String(40), default="sedentary")
    egfr: Mapped[float | None] = mapped_column(Float, nullable=True)
    ckd_stage: Mapped[str] = mapped_column(String(20), default="")
    ckd_risk: Mapped[str] = mapped_column(String(20), default="high")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NutritionMeal(Base):
    __tablename__ = "nutrition_meals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    meal_type: Mapped[str] = mapped_column(String(20), index=True)
    meal_date: Mapped[date] = mapped_column(Date, index=True)
    meal_time: Mapped[str] = mapped_column(String(8), default="")
    scenario_key: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NutritionFood(Base):
    __tablename__ = "nutrition_foods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    meal_id: Mapped[int] = mapped_column(ForeignKey("nutrition_meals.id"), index=True)
    food_ref_id: Mapped[str] = mapped_column(String(120), default="")
    food_name: Mapped[str] = mapped_column(String(255))
    portion: Mapped[str] = mapped_column(String(120), default="1인분")
    calories: Mapped[float] = mapped_column(Float, default=0.0)
    protein: Mapped[float] = mapped_column(Float, default=0.0)
    sodium: Mapped[float] = mapped_column(Float, default=0.0)
    fat: Mapped[float] = mapped_column(Float, default=0.0)
    carbohydrates: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DailyNutritionCheck(Base):
    __tablename__ = "daily_nutrition_checks"
    __table_args__ = (UniqueConstraint("patient_id", "check_date", name="uq_daily_nutrition_patient_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    check_date: Mapped[date] = mapped_column(Date, index=True)
    total_meals: Mapped[int] = mapped_column(Integer, default=0)
    threshold_calories: Mapped[float] = mapped_column(Float, default=0.0)
    threshold_protein: Mapped[float] = mapped_column(Float, default=0.0)
    threshold_sodium: Mapped[float] = mapped_column(Float, default=0.0)
    threshold_fat: Mapped[float] = mapped_column(Float, default=0.0)
    threshold_carbohydrates: Mapped[float] = mapped_column(Float, default=0.0)
    intake_calories: Mapped[float] = mapped_column(Float, default=0.0)
    intake_protein: Mapped[float] = mapped_column(Float, default=0.0)
    intake_sodium: Mapped[float] = mapped_column(Float, default=0.0)
    intake_fat: Mapped[float] = mapped_column(Float, default=0.0)
    intake_carbohydrates: Mapped[float] = mapped_column(Float, default=0.0)
    exceeded_calories: Mapped[bool] = mapped_column(Boolean, default=False)
    exceeded_protein: Mapped[bool] = mapped_column(Boolean, default=False)
    exceeded_sodium: Mapped[bool] = mapped_column(Boolean, default=False)
    exceeded_fat: Mapped[bool] = mapped_column(Boolean, default=False)
    exceeded_carbohydrates: Mapped[bool] = mapped_column(Boolean, default=False)
    excess_calories: Mapped[float] = mapped_column(Float, default=0.0)
    excess_protein: Mapped[float] = mapped_column(Float, default=0.0)
    excess_sodium: Mapped[float] = mapped_column(Float, default=0.0)
    excess_fat: Mapped[float] = mapped_column(Float, default=0.0)
    excess_carbohydrates: Mapped[float] = mapped_column(Float, default=0.0)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NutritionOntologyNode(Base):
    __tablename__ = "nutrition_ontology_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    node_key: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    node_type: Mapped[str] = mapped_column(String(40), index=True)
    label: Mapped[str] = mapped_column(String(255))
    normalized_label: Mapped[str] = mapped_column(String(255), index=True)
    source: Mapped[str] = mapped_column(String(80), default="seed")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NutritionOntologyTriple(Base):
    __tablename__ = "nutrition_ontology_triples"
    __table_args__ = (UniqueConstraint("subject_node_id", "predicate", "object_node_id", name="uq_nutrition_ontology_triple"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subject_node_id: Mapped[int] = mapped_column(ForeignKey("nutrition_ontology_nodes.id"), index=True)
    predicate: Mapped[str] = mapped_column(String(80), index=True)
    object_node_id: Mapped[int] = mapped_column(ForeignKey("nutrition_ontology_nodes.id"), index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(80), default="seed")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NutritionPatientPreferenceTriple(Base):
    __tablename__ = "nutrition_patient_preference_triples"
    __table_args__ = (UniqueConstraint("patient_id", "predicate", "object_node_id", name="uq_nutrition_patient_preference_triple"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    predicate: Mapped[str] = mapped_column(String(80), index=True)
    object_node_id: Mapped[int] = mapped_column(ForeignKey("nutrition_ontology_nodes.id"), index=True)
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    safety_level: Mapped[str] = mapped_column(String(20), default="soft", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(80), default="agent_tool")
    evidence_text: Mapped[str] = mapped_column(Text, default="")
    source_trace_id: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NutritionFoodRef(Base):
    __tablename__ = "nutrition_food_ref"

    food_ref_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    food_name: Mapped[str] = mapped_column(String(255), index=True)
    category: Mapped[str] = mapped_column(String(120), default="")
    serving_size: Mapped[float | None] = mapped_column(Float, nullable=True)
    energy: Mapped[float | None] = mapped_column(Float, nullable=True)
    carbohydrate: Mapped[float | None] = mapped_column(Float, nullable=True)
    protein: Mapped[float | None] = mapped_column(Float, nullable=True)
    fat: Mapped[float | None] = mapped_column(Float, nullable=True)
    sodium: Mapped[float | None] = mapped_column(Float, nullable=True)
    sugar: Mapped[float | None] = mapped_column(Float, nullable=True)
    cholesterol: Mapped[float | None] = mapped_column(Float, nullable=True)
    moisture: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(120), default="")
    manufacturer: Mapped[str] = mapped_column(String(120), default="")


class DoseSchedule(Base):
    __tablename__ = "dose_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("medication_plans.id"), index=True)
    slot_label: Mapped[str] = mapped_column(String(120))
    scheduled_time: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DoseEvent(Base):
    __tablename__ = "dose_events"
    __table_args__ = (UniqueConstraint("schedule_id", "scheduled_for", name="uq_schedule_datetime"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    plan_id: Mapped[int] = mapped_column(ForeignKey("medication_plans.id"), index=True)
    schedule_id: Mapped[int] = mapped_column(ForeignKey("dose_schedules.id"), index=True)
    medication_name: Mapped[str] = mapped_column(String(255))
    slot_label: Mapped[str] = mapped_column(String(120))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime, index=True)
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    alerts_generated: Mapped[bool] = mapped_column(Boolean, default=False)
    missed_handled: Mapped[bool] = mapped_column(Boolean, default=False)
    missed_detected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MissedDoseFlag(Base):
    __tablename__ = "missed_dose_flags"
    __table_args__ = (UniqueConstraint("patient_id", "flag_date", name="uq_missed_dose_flag_patient_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    flag_date: Mapped[date] = mapped_column(Date, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    trigger_slot_label: Mapped[str] = mapped_column(String(120), default="")
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    clear_reason: Mapped[str] = mapped_column(Text, default="")
    subsequent_taken_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ReminderPolicy(Base):
    __tablename__ = "reminder_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    policy_key: Mapped[str] = mapped_column(String(120), default="custom")
    slot_label: Mapped[str] = mapped_column(String(120), index=True)
    extra_reminders: Mapped[int] = mapped_column(Integer, default=0)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=5)
    missed_dose_after_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    primary_reminder_timing: Mapped[str] = mapped_column(String(20), default="at")
    primary_reminder_offset_minutes: Mapped[int] = mapped_column(Integer, default=0)
    medication_title_template: Mapped[str] = mapped_column(Text, default="{medication_name} 복약 알림")
    medication_body_template: Mapped[str] = mapped_column(Text, default="{slot_label} 복약 시간입니다. 복약 후 기록 버튼을 눌러주세요.")
    extra_title_template: Mapped[str] = mapped_column(Text, default="{medication_name} 추가 복약 알림")
    extra_body_template: Mapped[str] = mapped_column(Text, default="{slot_label} 복약을 놓치지 않도록 다시 알려드려요.")
    missed_dose_title_template: Mapped[str] = mapped_column(Text, default="미복용 AI 알림")
    missed_dose_body_template: Mapped[str] = mapped_column(
        Text,
        default="{slot_label} {medication_name} 미복용이 확정되어 AI가 상황을 확인하고 있어요.",
    )
    effective_start_date: Mapped[date] = mapped_column(Date, index=True)
    effective_end_date: Mapped[date] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(40))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SystemPolicyOverride(Base):
    __tablename__ = "system_policy_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    policy_key: Mapped[str] = mapped_column(String(120), index=True)
    value: Mapped[str] = mapped_column(String(120))
    reason: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(40), default="patient_request")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    notification_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    visible_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="demo-patient")
    role: Mapped[str] = mapped_column(String(20))
    sender_type: Mapped[str] = mapped_column(String(20), default="user")
    category: Mapped[str] = mapped_column(String(40), default="chat")
    content: Mapped[str] = mapped_column(Text)
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimulationClock(Base):
    __tablename__ = "simulation_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_time: Mapped[datetime] = mapped_column(DateTime)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    speed_multiplier: Mapped[int] = mapped_column(Integer, default=0)
    last_tick_real_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_processed_sim_time: Mapped[datetime] = mapped_column(DateTime)
    last_daily_pattern_sent_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentDecisionAudit(Base):
    __tablename__ = "agent_decision_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(120), index=True)
    agent_name: Mapped[str] = mapped_column(String(120))
    prompt_version_id: Mapped[str] = mapped_column(String(120))
    decision_type: Mapped[str] = mapped_column(String(80))
    structured_payload: Mapped[str] = mapped_column(Text, default="{}")
    human_summary: Mapped[str] = mapped_column(Text, default="")
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    error_message: Mapped[str] = mapped_column(Text, default="")
    source_event_type: Mapped[str] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentRunTrace(Base):
    __tablename__ = "agent_run_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(180), default="", index=True)
    patient_id_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    workflow_name: Mapped[str] = mapped_column(String(120), index=True)
    source_event_type: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="completed", index=True)
    agent_name: Mapped[str] = mapped_column(String(120), default="")
    decision_type: Mapped[str] = mapped_column(String(80), default="")
    prompt_version_id: Mapped[str] = mapped_column(String(120), default="")
    provider: Mapped[str] = mapped_column(String(80), default="")
    model_tier: Mapped[str] = mapped_column(String(40), default="")
    model_id: Mapped[str] = mapped_column(String(255), default="")
    input_hash: Mapped[str] = mapped_column(String(64), default="")
    output_hash: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tool_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AgentRunStep(Base):
    __tablename__ = "agent_run_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_id: Mapped[str] = mapped_column(String(120), index=True)
    step_type: Mapped[str] = mapped_column(String(60), index=True)
    step_name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(40), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    tool_name: Mapped[str] = mapped_column(String(120), default="")
    side_effect_level: Mapped[str] = mapped_column(String(40), default="")
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_type: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="pending")
    payload_json: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
