from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import (
    AgentDecisionAudit,
    AgentJob,
    ChatMessage,
    DailyNutritionCheck,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    Notification,
    NutritionFood,
    NutritionMeal,
    ReminderPolicy,
    SideEffectRecord,
    SimulationPatientProfile,
    SystemPolicyOverride,
)
from system_app.services.clock_service import ensure_clock, parse_clock_value
from system_app.services.patient_profile_service import ensure_base_data, mark_phr_sync_needed
from system_app.services.simulation_constants import parse_times_csv, slot_label_for_time

settings = get_settings()


def create_medication_plan(
    session: Session,
    medication_name: str,
    dosage: str,
    start_date: date,
    end_date: date,
    times_csv: str,
    instructions: str = "",
) -> MedicationPlan:
    from system_app.services.dose_event_service import ensure_day_events

    clock = ensure_clock(session)
    plan = MedicationPlan(
        patient_id=settings.patient_id,
        medication_name=medication_name,
        dosage=dosage,
        instructions=instructions,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(plan)
    session.flush()
    for schedule_time in parse_times_csv(times_csv):
        session.add(
            DoseSchedule(
                plan_id=plan.id,
                slot_label=slot_label_for_time(schedule_time),
                scheduled_time=schedule_time,
            )
        )
    session.flush()
    if start_date <= clock.current_time.date() <= end_date:
        ensure_day_events(session, clock.current_time.date())
    else:
        ensure_day_events(session, start_date)
    mark_phr_sync_needed(session)
    session.commit()
    session.refresh(plan)
    return plan

def reset_simulation_state(session: Session) -> None:
    initial_time = parse_clock_value(settings.simulation_initial_time)
    clock = ensure_clock(session)

    session.execute(delete(Notification))
    session.execute(delete(ChatMessage))
    session.execute(delete(AgentDecisionAudit))
    session.execute(delete(AgentJob))
    session.execute(delete(MissedDoseFlag))
    session.execute(delete(SideEffectRecord))
    session.execute(delete(DailyNutritionCheck))
    session.execute(delete(NutritionFood))
    session.execute(delete(NutritionMeal))
    session.execute(delete(DoseEvent))
    session.execute(delete(DoseSchedule))
    session.execute(delete(ReminderPolicy))
    session.execute(delete(SystemPolicyOverride))
    session.execute(delete(SimulationPatientProfile))
    session.execute(delete(MedicationPlan))

    clock.current_time = initial_time
    clock.is_running = False
    clock.speed_multiplier = 0
    clock.last_tick_real_at = utc_now()
    clock.last_processed_sim_time = initial_time
    clock.last_daily_pattern_sent_date = initial_time.date() - timedelta(days=1)

    session.flush()
    ensure_base_data(session)
    session.commit()

def delete_medication_plan(session: Session, plan_id: int) -> bool:
    plan = session.get(MedicationPlan, plan_id)
    if plan is None:
        return False

    schedules = session.scalars(select(DoseSchedule).where(DoseSchedule.plan_id == plan_id)).all()
    slot_labels = sorted({row.slot_label for row in schedules})
    dose_event_ids = session.scalars(select(DoseEvent.id).where(DoseEvent.plan_id == plan_id)).all()

    if dose_event_ids:
        session.execute(delete(Notification).where(Notification.related_dose_event_id.in_(dose_event_ids)))
        session.execute(delete(ChatMessage).where(ChatMessage.related_dose_event_id.in_(dose_event_ids)))
        session.execute(delete(MissedDoseFlag).where(MissedDoseFlag.related_dose_event_id.in_(dose_event_ids)))

    session.execute(delete(DoseEvent).where(DoseEvent.plan_id == plan_id))
    session.execute(delete(DoseSchedule).where(DoseSchedule.plan_id == plan_id))
    session.execute(delete(MedicationPlan).where(MedicationPlan.id == plan_id))

    for slot_label in slot_labels:
        remaining_count = session.scalar(select(func.count(DoseSchedule.id)).where(DoseSchedule.slot_label == slot_label)) or 0
        if remaining_count == 0:
            for policy in session.scalars(
                select(ReminderPolicy).where(
                    ReminderPolicy.patient_id == settings.patient_id,
                    ReminderPolicy.slot_label == slot_label,
                    ReminderPolicy.active.is_(True),
                )
            ).all():
                policy.active = False
                policy.updated_at = utc_now()

    mark_phr_sync_needed(session)
    session.commit()
    return True

def list_medication_plans(session: Session) -> Sequence[MedicationPlan]:
    stmt = select(MedicationPlan).order_by(MedicationPlan.created_at.desc())
    return session.scalars(stmt).all()

def get_schedule_map(session: Session) -> dict[int, list[DoseSchedule]]:
    rows = session.scalars(select(DoseSchedule).order_by(DoseSchedule.scheduled_time.asc())).all()
    grouped: dict[int, list[DoseSchedule]] = defaultdict(list)
    for row in rows:
        grouped[row.plan_id].append(row)
    return grouped

def get_schedule_slot_labels(session: Session) -> list[str]:
    return list(
        session.scalars(
            select(DoseSchedule.slot_label)
            .join(MedicationPlan, MedicationPlan.id == DoseSchedule.plan_id)
            .where(MedicationPlan.active.is_(True))
            .distinct()
            .order_by(DoseSchedule.scheduled_time.asc())
        ).all()
    )
