from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
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
    MutationConfirmation,
    Notification,
    NutritionFood,
    NutritionMeal,
    NutritionPatientPreferenceTriple,
    ReminderPolicy,
    SideEffectRecord,
    SimulationPatientProfile,
    SystemPolicyOverride,
)
from system_app.services.clock_service import ensure_clock, parse_clock_value
from system_app.services.patient_profile_service import ensure_base_data, mark_phr_sync_needed
from system_app.services.simulation_constants import parse_times_csv, slot_label_for_time

settings = get_settings()


class MedicationSubmissionConflictError(RuntimeError):
    code = "medication_submission_conflict"


def normalize_medication_submission_id(submission_id: str | None) -> str | None:
    """Return a canonical UUID or ``None`` for legacy form submissions."""

    raw_value = (submission_id or "").strip()
    if not raw_value:
        return None
    if len(raw_value) > 36:
        raise ValueError("medication_submission_id_invalid")
    try:
        return str(UUID(raw_value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("medication_submission_id_invalid") from exc


def _existing_submission_plan(
    session: Session,
    *,
    patient_id: str,
    submission_id: str,
) -> MedicationPlan | None:
    return session.scalar(
        select(MedicationPlan).where(
            MedicationPlan.patient_id == patient_id,
            MedicationPlan.submission_id == submission_id,
        )
    )


def _is_same_medication_submission(
    session: Session,
    plan: MedicationPlan,
    *,
    medication_name: str,
    dosage: str,
    start_date: date,
    end_date: date,
    schedule_times: Sequence[str],
    instructions: str,
) -> bool:
    persisted_times = tuple(
        session.scalars(
            select(DoseSchedule.scheduled_time)
            .where(DoseSchedule.plan_id == plan.id)
            .order_by(DoseSchedule.id.asc())
        ).all()
    )
    return (
        plan.medication_name == medication_name
        and plan.dosage == dosage
        and plan.start_date == start_date
        and plan.end_date == end_date
        and plan.instructions == instructions
        and persisted_times == tuple(schedule_times)
    )


def _resolve_medication_submission_replay(
    session: Session,
    plan: MedicationPlan,
    *,
    medication_name: str,
    dosage: str,
    start_date: date,
    end_date: date,
    schedule_times: Sequence[str],
    instructions: str,
) -> MedicationPlan:
    if _is_same_medication_submission(
        session,
        plan,
        medication_name=medication_name,
        dosage=dosage,
        start_date=start_date,
        end_date=end_date,
        schedule_times=schedule_times,
        instructions=instructions,
    ):
        return plan
    raise MedicationSubmissionConflictError


def create_medication_plan(
    session: Session,
    medication_name: str,
    dosage: str,
    start_date: date,
    end_date: date,
    times_csv: str,
    instructions: str = "",
    submission_id: str | None = None,
) -> MedicationPlan:
    from system_app.services.dose_event_service import ensure_day_events

    patient_id = settings.patient_id
    normalized_submission_id = normalize_medication_submission_id(submission_id)
    schedule_times = parse_times_csv(times_csv)
    if normalized_submission_id is not None:
        existing_plan = _existing_submission_plan(
            session,
            patient_id=patient_id,
            submission_id=normalized_submission_id,
        )
        if existing_plan is not None:
            return _resolve_medication_submission_replay(
                session,
                existing_plan,
                medication_name=medication_name,
                dosage=dosage,
                start_date=start_date,
                end_date=end_date,
                schedule_times=schedule_times,
                instructions=instructions,
            )

    clock = ensure_clock(session)
    plan = MedicationPlan(
        patient_id=patient_id,
        submission_id=normalized_submission_id,
        medication_name=medication_name,
        dosage=dosage,
        instructions=instructions,
        start_date=start_date,
        end_date=end_date,
    )
    if normalized_submission_id is None:
        session.add(plan)
        session.flush()
    else:
        try:
            with session.begin_nested():
                session.add(plan)
                session.flush()
        except IntegrityError:
            existing_plan = _existing_submission_plan(
                session,
                patient_id=patient_id,
                submission_id=normalized_submission_id,
            )
            if existing_plan is None:
                raise
            return _resolve_medication_submission_replay(
                session,
                existing_plan,
                medication_name=medication_name,
                dosage=dosage,
                start_date=start_date,
                end_date=end_date,
                schedule_times=schedule_times,
                instructions=instructions,
            )

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
    mark_phr_sync_needed(session)
    session.commit()
    session.refresh(plan)
    return plan

def reset_simulation_state(session: Session) -> None:
    initial_time = parse_clock_value(settings.simulation_initial_time)
    clock = ensure_clock(session)

    # Confirmations reference notifications and chat messages, so clear them
    # before their parent rows. This ordering is required by PostgreSQL and by
    # SQLite when foreign-key enforcement is enabled.
    session.execute(delete(MutationConfirmation))
    session.execute(delete(Notification))
    session.execute(delete(ChatMessage))
    session.execute(delete(AgentDecisionAudit))
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

        # Confirmations are also retained as history. Their nullable UI-origin
        # references must be cleared before the source rows are deleted.
        if notification_ids:
            session.execute(
                update(MutationConfirmation)
                .where(MutationConfirmation.origin_request_notification_id.in_(notification_ids))
                .values(origin_request_notification_id=None)
            )
        if chat_message_ids:
            session.execute(
                update(MutationConfirmation)
                .where(MutationConfirmation.chat_message_id.in_(chat_message_ids))
                .values(chat_message_id=None)
            )
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
