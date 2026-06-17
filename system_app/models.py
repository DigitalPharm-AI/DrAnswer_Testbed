from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.utcnow()


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
