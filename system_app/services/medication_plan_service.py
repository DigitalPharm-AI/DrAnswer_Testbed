from __future__ import annotations

from collections import defaultdict
from datetime import date

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import (
    AgentJob,
    ChatMessage,
    DailyNutritionCheck,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    Notification,
    NotificationPolicyChangeProposal,
    NutritionFood,
    NutritionMeal,
    NutritionPatientPreferenceTriple,
    ReminderPolicy,
    SideEffectRecord,
    SystemPolicyOverride,
)
from system_app.services.clock_service import ensure_clock, parse_clock_value
from system_app.services.patient_profile_service import ensure_base_data
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

    patient_id = settings.patient_id
    schedule_times = parse_times_csv(times_csv)

    clock = ensure_clock(session)
    plan = MedicationPlan(
        patient_id=patient_id,
        medication_name=medication_name,
        dosage=dosage,
        instructions=instructions,
        start_date=start_date,
        end_date=end_date,
    )
    session.add(plan)
    session.flush()

    for schedule_time in schedule_times:
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
    session.commit()
    session.refresh(plan)
    return plan

def reset_simulation_state(
    session: Session,
    *,
    commit: bool = True,
) -> None:
    initial_time = parse_clock_value(settings.simulation_initial_time)
    clock = ensure_clock(session)

    session.execute(delete(NotificationPolicyChangeProposal))
    session.execute(delete(Notification))
    session.execute(delete(ChatMessage))
    session.execute(delete(AgentJob))
    session.execute(delete(MissedDoseFlag))
    session.execute(delete(SideEffectRecord))
    session.execute(delete(DailyNutritionCheck))
    session.execute(delete(NutritionFood))
    session.execute(delete(NutritionMeal))
    session.execute(delete(NutritionPatientPreferenceTriple))
    session.execute(delete(DoseEvent))
    session.execute(delete(DoseSchedule))
    session.execute(delete(ReminderPolicy))
    session.execute(delete(SystemPolicyOverride))
    session.execute(delete(MedicationPlan))

    clock.current_time = initial_time
    clock.is_running = False
    clock.speed_multiplier = 0
    clock.last_tick_real_at = utc_now()
    clock.last_processed_sim_time = initial_time

    session.flush()
    ensure_base_data(session)
    if commit:
        session.commit()
    else:
        session.flush()

def delete_medication_plan(session: Session, plan_id: int) -> bool:
    plan = session.get(MedicationPlan, plan_id)
    if plan is None:
        return False

    schedules = session.scalars(select(DoseSchedule).where(DoseSchedule.plan_id == plan_id)).all()
    slot_labels = sorted({row.slot_label for row in schedules})
    dose_event_ids = session.scalars(select(DoseEvent.id).where(DoseEvent.plan_id == plan_id)).all()

    if dose_event_ids:
        notification_ids = session.scalars(
            select(Notification.id).where(Notification.related_dose_event_id.in_(dose_event_ids))
        ).all()
        chat_message_ids = session.scalars(
            select(ChatMessage.id).where(ChatMessage.related_dose_event_id.in_(dose_event_ids))
        ).all()

        # Agent jobs and side-effect records are operational/clinical history.
        # Keep those rows while severing the reference to the removed event.
        session.execute(
            update(AgentJob)
            .where(AgentJob.related_dose_event_id.in_(dose_event_ids))
            .values(related_dose_event_id=None)
        )
        session.execute(
            update(SideEffectRecord)
            .where(SideEffectRecord.related_dose_event_id.in_(dose_event_ids))
            .values(related_dose_event_id=None)
        )

        if chat_message_ids:
            # A later conversation message can reply to a dose-linked message
            # without itself being dose-linked. Preserve it and clear the
            # self-referential foreign key before removing its parent.
            session.execute(
                update(ChatMessage)
                .where(ChatMessage.reply_to_message_id.in_(chat_message_ids))
                .values(reply_to_message_id=None)
            )

        session.execute(delete(Notification).where(Notification.id.in_(notification_ids)))
        session.execute(delete(ChatMessage).where(ChatMessage.id.in_(chat_message_ids)))
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

    session.commit()
    return True

def get_schedule_map(session: Session) -> dict[int, list[DoseSchedule]]:
    rows = session.scalars(select(DoseSchedule).order_by(DoseSchedule.scheduled_time.asc())).all()
    grouped: dict[int, list[DoseSchedule]] = defaultdict(list)
    for row in rows:
        grouped[row.plan_id].append(row)
    return grouped
