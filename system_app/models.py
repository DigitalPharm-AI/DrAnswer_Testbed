from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from shared.public_ids import PublicIdKind, new_public_id, require_public_id
from shared.retention_policy import agent_observability_expires_at
from shared.time_utils import utc_now

PUBLIC_ID_ALLOCATION_ATTEMPTS = 8


def utcnow() -> datetime:
    return utc_now()


def observability_expires_at() -> datetime:
    return agent_observability_expires_at(utc_now())


def new_message_public_id(role: str) -> str:
    normalized_role = str(role).strip().lower()
    if normalized_role == "user":
        return new_public_id("user_message")
    if normalized_role == "assistant":
        return new_public_id("assistant_message")
    raise ValueError("message_public_id_role_must_be_user_or_assistant")


def new_notification_public_id() -> str:
    return f"notif_{uuid4().hex}"


def _allocate_contract_id(
    connection,
    *,
    table_name: str,
    column_name: str,
    kind: PublicIdKind,
) -> str:
    """Allocate a collision-safe public ID inside the current transaction."""

    for _attempt in range(PUBLIC_ID_ALLOCATION_ATTEMPTS):
        candidate = new_public_id(kind)
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:candidate, 0))"
                ),
                {"candidate": candidate},
            )
        exists = connection.execute(
            text(
                f"SELECT 1 FROM {table_name} "
                f"WHERE {column_name} = :candidate LIMIT 1"
            ),
            {"candidate": candidate},
        ).first()
        if exists is None:
            return candidate
    raise RuntimeError(
        f"public_id_collision_retry_exhausted:{table_name}:{kind}"
    )


class Base(DeclarativeBase):
    pass


class MedicationPlan(Base):
    __tablename__ = "medication_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
    medication_name: Mapped[str] = mapped_column(String(255))
    dosage: Mapped[str] = mapped_column(String(255), default="")
    instructions: Mapped[str] = mapped_column(Text, default="")
    treatment_area: Mapped[str] = mapped_column(String(80), default="")
    source_type: Mapped[str] = mapped_column(String(40), default="manual", index=True)
    source_key: Mapped[str] = mapped_column(String(120), default="", index=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TestMedicationScenario(Base):
    __tablename__ = "test_medication_scenarios"

    scenario_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TestMedicationScenarioItem(Base):
    __tablename__ = "test_medication_scenario_items"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id",
            "medication_id",
            name="uq_test_medication_scenario_item",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scenario_id: Mapped[str] = mapped_column(
        ForeignKey("test_medication_scenarios.scenario_id"),
        index=True,
    )
    medication_id: Mapped[str] = mapped_column(String(80))
    medication_name: Mapped[str] = mapped_column(String(255))
    dosage: Mapped[str] = mapped_column(String(255), default="")
    treatment_area: Mapped[str] = mapped_column(String(80))
    slot_label: Mapped[str] = mapped_column(String(120))
    scheduled_time: Mapped[str] = mapped_column(String(8))
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NutritionProfile(Base):
    __tablename__ = "nutrition_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), unique=True, index=True, default="patient_0000000000000001")
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
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
    meal_type: Mapped[str] = mapped_column(String(20), index=True)
    meal_date: Mapped[date] = mapped_column(Date, index=True)
    meal_time: Mapped[str] = mapped_column(String(8), default="")
    scenario_key: Mapped[str] = mapped_column(String(80), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NutritionFood(Base):
    __tablename__ = "nutrition_foods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    meal_id: Mapped[int] = mapped_column(ForeignKey("nutrition_meals.id"), index=True)
    food_ref_id: Mapped[str] = mapped_column(String(120), default="")
    food_name: Mapped[str] = mapped_column(String(255))
    portion: Mapped[str] = mapped_column(String(120), default="1인분")
    calories: Mapped[float] = mapped_column(Float, default=0.0)
    protein: Mapped[float] = mapped_column(Float, default=0.0)
    sodium: Mapped[float] = mapped_column(Float, default=0.0)
    fat: Mapped[float] = mapped_column(Float, default=0.0)
    carbohydrates: Mapped[float] = mapped_column(Float, default=0.0)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class DailyNutritionCheck(Base):
    __tablename__ = "daily_nutrition_checks"
    __table_args__ = (UniqueConstraint("patient_id", "check_date", name="uq_daily_nutrition_patient_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
    predicate: Mapped[str] = mapped_column(String(80), index=True)
    object_node_id: Mapped[int] = mapped_column(ForeignKey("nutrition_ontology_nodes.id"), index=True)
    strength: Mapped[float] = mapped_column(Float, default=1.0)
    safety_level: Mapped[str] = mapped_column(String(20), default="soft", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    source: Mapped[str] = mapped_column(String(80), default="agent_tool")
    evidence_text: Mapped[str] = mapped_column(Text, default="")
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
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SideEffectRecord(Base):
    __tablename__ = "side_effect_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
    medication_name: Mapped[str] = mapped_column(String(255), index=True, default="")
    symptom_text: Mapped[str] = mapped_column(Text, default="")
    symptom_onset_text: Mapped[str] = mapped_column(Text, default="")
    suspected: Mapped[bool] = mapped_column(Boolean, index=True, default=False)
    severity_result_json: Mapped[str] = mapped_column(
        Text,
        default='{"questions":[],"responses":[]}',
    )
    matched_effects_json: Mapped[str] = mapped_column(Text, default="[]")
    matched_items_json: Mapped[str] = mapped_column(Text, default="[]")
    evidence: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    source_event_type: Mapped[str] = mapped_column(String(80), default="agent_tool")
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MissedDoseFlag(Base):
    __tablename__ = "missed_dose_flags"
    __table_args__ = (UniqueConstraint("patient_id", "flag_date", name="uq_missed_dose_flag_patient_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SystemPolicyOverride(Base):
    __tablename__ = "system_policy_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
        default=new_notification_public_id,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
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
    __table_args__ = (
        Index(
            "ix_chat_messages_patient_display_at",
            "patient_id",
            "display_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    public_id: Mapped[str] = mapped_column(
        String(80),
        unique=True,
        index=True,
    )
    patient_id: Mapped[str] = mapped_column(String(100), index=True, default="patient_0000000000000001")
    ai_request_id: Mapped[str] = mapped_column(String(180), index=True, default="")
    role: Mapped[str] = mapped_column(String(20))
    sender_type: Mapped[str] = mapped_column(String(20), default="user")
    category: Mapped[str] = mapped_column(String(40), default="chat")
    message_type: Mapped[str] = mapped_column(String(32), default="text")
    content: Mapped[str] = mapped_column(Text)
    message_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    reply_to_message_id: Mapped[int | None] = mapped_column(ForeignKey("chat_messages.id"), nullable=True, index=True)
    processing_status: Mapped[str] = mapped_column(String(32), default="completed", index=True)
    related_dose_event_id: Mapped[int | None] = mapped_column(ForeignKey("dose_events.id"), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    display_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
    )


@event.listens_for(ChatMessage, "before_insert")
def _set_chat_message_display_at(
    _mapper,
    connection,
    target: ChatMessage,
) -> None:
    role = str(target.role or "").strip().lower()
    kind: PublicIdKind
    if role == "user":
        kind = "user_message"
    elif role == "assistant":
        kind = "assistant_message"
    else:
        raise ValueError(
            "message_public_id_role_must_be_user_or_assistant"
        )
    if target.public_id:
        require_public_id(target.public_id, kind)
    else:
        target.public_id = _allocate_contract_id(
            connection,
            table_name="chat_messages",
            column_name="public_id",
            kind=kind,
        )
    if target.display_at is not None:
        return
    target.display_at = target.created_at or utcnow()


class BackendApiRequest(Base):
    __tablename__ = "backend_api_requests"
    __table_args__ = (UniqueConstraint("api_path", "request_id", name="uq_backend_api_path_request"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    api_path: Mapped[str] = mapped_column(String(180), index=True)
    request_id: Mapped[str] = mapped_column(String(180), index=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="PROCESSING", index=True)
    http_status: Mapped[int] = mapped_column(Integer, default=0)
    response_json: Mapped[str] = mapped_column(Text, default="{}")
    error_code: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SimulationClock(Base):
    __tablename__ = "simulation_clock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    current_time: Mapped[datetime] = mapped_column(DateTime)
    is_running: Mapped[bool] = mapped_column(Boolean, default=False)
    speed_multiplier: Mapped[int] = mapped_column(Integer, default=0)
    last_tick_real_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_processed_sim_time: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentAsyncCallbackReceipt(Base):
    """DB-level idempotency receipt for the external async result callback."""

    __tablename__ = "agent_async_callback_receipts"

    request_id: Mapped[str] = mapped_column(
        String(180),
        primary_key=True,
    )
    callback_hash: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(60))
    result_status: Mapped[str] = mapped_column(String(24))
    processed_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=observability_expires_at,
        index=True,
    )


class NotificationPolicyChangeProposal(Base):
    """Patient-scoped proposal receipt; it never represents an applied policy."""

    __tablename__ = "notification_policy_change_proposals"

    request_id: Mapped[str] = mapped_column(
        String(180),
        primary_key=True,
    )
    callback_hash: Mapped[str] = mapped_column(String(64))
    patient_id: Mapped[str] = mapped_column(String(100), index=True)
    proposed_policy_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(32),
        default="pending_user_confirmation",
        index=True,
    )
    notification_id: Mapped[int | None] = mapped_column(
        ForeignKey("notifications.id"),
        nullable=True,
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
    )


class AgentJob(Base):
    __tablename__ = "agent_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(32),
        unique=True,
        index=True,
    )
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


def _contract_id_before_insert(
    *,
    table_name: str,
    column_name: str,
    kind: PublicIdKind,
):
    def assign(_mapper, connection, target) -> None:
        current = getattr(target, column_name, None)
        if current:
            require_public_id(str(current), kind)
            return
        setattr(
            target,
            column_name,
            _allocate_contract_id(
                connection,
                table_name=table_name,
                column_name=column_name,
                kind=kind,
            ),
        )

    return assign


event.listen(
    NutritionMeal,
    "before_insert",
    _contract_id_before_insert(
        table_name="nutrition_meals",
        column_name="public_id",
        kind="meal",
    ),
)
event.listen(
    NutritionFood,
    "before_insert",
    _contract_id_before_insert(
        table_name="nutrition_foods",
        column_name="public_id",
        kind="food",
    ),
)
event.listen(
    DoseEvent,
    "before_insert",
    _contract_id_before_insert(
        table_name="dose_events",
        column_name="public_id",
        kind="dose_event",
    ),
)
event.listen(
    SideEffectRecord,
    "before_insert",
    _contract_id_before_insert(
        table_name="side_effect_records",
        column_name="public_id",
        kind="side_effect",
    ),
)
event.listen(
    ReminderPolicy,
    "before_insert",
    _contract_id_before_insert(
        table_name="reminder_policies",
        column_name="public_id",
        kind="notification_policy",
    ),
)
event.listen(
    AgentJob,
    "before_insert",
    _contract_id_before_insert(
        table_name="agent_jobs",
        column_name="request_id",
        kind="request",
    ),
)
