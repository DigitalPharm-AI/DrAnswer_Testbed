from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object as parse_metadata_json
from shared.schemas import (
    AgentCallbackContext,
    ChatTurn,
    MissedDoseEventPayload,
    MissedDoseReplyContext,
    SlotAdherenceSummary,
)
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import ChatMessage, DoseEvent, MedicationPlan, Notification, SimulationClock
from system_app.services.clock_service import (
    ensure_clock,
    pause_simulation_clock_at_conversation,
)
from system_app.services.medication_plan_service import get_schedule_map
from system_app.services.missed_dose_flag_service import activate_missed_dose_flag, clear_missed_dose_flag_after_taken
from system_app.services.notification_service import create_notification, get_unacknowledged_conversation_alert
from system_app.services.policy_service import (
    extra_reminder_visible_at,
    max_primary_reminder_lead_minutes,
    missed_dose_due_at_for_event,
    primary_reminder_visible_at,
    render_policy_template,
    resolve_policy_boundary_for_event,
    resolve_policy_for_event,
)
from system_app.services.side_effect_reminder_safety import is_reminder_suppressed_after_side_effect
from system_app.services.ui_policy_service import (
    MEDICATION_SCHEDULE_ALERT,
    MISSED_DOSE_CONVERSATION,
    ui_policy_enabled,
)

settings = get_settings()
MISSED_DOSE_PENDING_REPLY_HINTS = (
    "잠시 후 채팅에서 답변할 수 있습니다.",
    "잠시 후 이 알림 안에서 답변할 수 있습니다.",
)


def missed_dose_pending_body(session: Session, event: DoseEvent, policy=None) -> str:
    policy = policy or resolve_policy_for_event(session, event)
    body = render_policy_template(
        policy.missed_dose_body_template,
        medication_name=event.medication_name,
        slot_label=event.slot_label,
        scheduled_for=event.scheduled_for,
    )
    for hint in MISSED_DOSE_PENDING_REPLY_HINTS:
        body = body.replace(hint, "")
    return " ".join(body.split()).strip()


def ensure_day_events(session: Session, target_date: date) -> None:
    start = datetime.combine(target_date, time.min)
    end = datetime.combine(target_date, time.max)
    generate_due_dose_events(session, start, end)
    session.flush()

def create_missed_dose_conversation_alert(session: Session, event: DoseEvent, visible_at: datetime) -> Notification:
    activate_missed_dose_flag(session, event, activated_at=visible_at)
    existing = get_unacknowledged_conversation_alert(session, event.id)
    if existing is not None:
        return existing
    _, resume_state = pause_simulation_clock_at_conversation(session, visible_at)
    policy = resolve_policy_for_event(session, event)
    notification = create_notification(
        session,
        notification_type="conversation_alert",
        title="AI가 대화를 요청합니다.",
        body=missed_dose_pending_body(session, event, policy),
        visible_at=visible_at,
        related_dose_event_id=event.id,
        patient_id=event.patient_id,
        metadata={
            "category": "missed_dose",
            "status": "awaiting_agent",
            "resume_clock": resume_state,
            "policy_key": policy.policy_key,
            "policy_source": policy.source,
        },
    )
    return notification

def find_first_missed_dose_due(session: Session, up_to_time: datetime) -> datetime | None:
    stmt = select(DoseEvent).where(
        DoseEvent.patient_id == settings.patient_id,
        DoseEvent.status == "scheduled",
        DoseEvent.missed_handled.is_(False),
    )
    due_times = [due_at for event in session.scalars(stmt).all() if (due_at := missed_dose_due_at_for_event(session, event)) <= up_to_time]
    return min(due_times) if due_times else None

def generate_due_dose_events(session: Session, start_dt: datetime, end_dt: datetime) -> None:
    if end_dt <= start_dt:
        return
    date_cursor = start_dt.date()
    final_date = end_dt.date()
    plans = session.scalars(
        select(MedicationPlan).where(
            MedicationPlan.patient_id == settings.patient_id,
            MedicationPlan.active.is_(True),
        )
    ).all()
    schedules = get_schedule_map(session)

    while date_cursor <= final_date:
        for plan in plans:
            if not (plan.start_date <= date_cursor <= plan.end_date):
                continue
            for schedule in schedules.get(plan.id, []):
                scheduled_for = datetime.combine(date_cursor, datetime.strptime(schedule.scheduled_time, "%H:%M").time())
                existing = session.scalar(
                    select(DoseEvent).where(
                        DoseEvent.patient_id == plan.patient_id,
                        DoseEvent.schedule_id == schedule.id,
                        DoseEvent.scheduled_for == scheduled_for,
                    )
                )
                if existing:
                    continue
                if scheduled_for > end_dt:
                    continue
                session.add(
                    DoseEvent(
                        patient_id=plan.patient_id,
                        plan_id=plan.id,
                        schedule_id=schedule.id,
                        medication_name=plan.medication_name,
                        slot_label=schedule.slot_label,
                        scheduled_for=scheduled_for,
                        status="scheduled",
                    )
                )
        date_cursor += timedelta(days=1)
    session.flush()

def generate_notifications_for_new_events(session: Session, end_dt: datetime) -> None:
    reminder_suppressed = is_reminder_suppressed_after_side_effect(session)
    medication_alert_enabled = ui_policy_enabled(
        session,
        MEDICATION_SCHEDULE_ALERT,
        patient_id=settings.patient_id,
    )
    stmt = select(DoseEvent).where(
        DoseEvent.patient_id == settings.patient_id,
        DoseEvent.alerts_generated.is_(False),
    )
    for event in session.scalars(stmt).all():
        policy = resolve_policy_for_event(session, event)
        primary_visible_at = primary_reminder_visible_at(policy, event.scheduled_for)
        if primary_visible_at > end_dt:
            continue
        if reminder_suppressed or not medication_alert_enabled:
            event.alerts_generated = True
            continue

        create_notification(
            session,
            notification_type="medication_alert",
            title=render_policy_template(
                policy.medication_title_template,
                medication_name=event.medication_name,
                slot_label=event.slot_label,
                scheduled_for=event.scheduled_for,
            ),
            body=render_policy_template(
                policy.medication_body_template,
                medication_name=event.medication_name,
                slot_label=event.slot_label,
                scheduled_for=event.scheduled_for,
            ),
            visible_at=primary_visible_at,
            related_dose_event_id=event.id,
            patient_id=event.patient_id,
            metadata={"sequence": 1, "slot_label": event.slot_label, "policy_key": policy.policy_key, "policy_source": policy.source},
        )
        for index in range(policy.extra_reminders):
            create_notification(
                session,
                notification_type="medication_alert",
                title=render_policy_template(
                    policy.extra_title_template,
                    medication_name=event.medication_name,
                    slot_label=event.slot_label,
                    scheduled_for=event.scheduled_for,
                ),
                body=render_policy_template(
                    policy.extra_body_template,
                    medication_name=event.medication_name,
                    slot_label=event.slot_label,
                    scheduled_for=event.scheduled_for,
                ),
                visible_at=extra_reminder_visible_at(policy, event.scheduled_for, index),
                related_dose_event_id=event.id,
                patient_id=event.patient_id,
                metadata={
                    "sequence": index + 2,
                    "slot_label": event.slot_label,
                    "policy_key": policy.policy_key,
                    "policy_source": policy.source,
                },
            )
        event.alerts_generated = True
    session.flush()

def build_slot_summaries(events: Iterable[DoseEvent]) -> list[SlotAdherenceSummary]:
    grouped: dict[str, dict[str, int]] = defaultdict(lambda: {"scheduled": 0, "taken": 0, "missed": 0})
    for event in events:
        grouped[event.slot_label]["scheduled"] += 1
        if event.status == "taken":
            grouped[event.slot_label]["taken"] += 1
        if event.status == "missed":
            grouped[event.slot_label]["missed"] += 1

    summaries: list[SlotAdherenceSummary] = []
    for slot_label, counts in grouped.items():
        scheduled_count = counts["scheduled"]
        missed_count = counts["missed"]
        summaries.append(
            SlotAdherenceSummary(
                slot_label=slot_label,
                scheduled_count=scheduled_count,
                taken_count=counts["taken"],
                missed_count=missed_count,
                miss_rate=(missed_count / scheduled_count) if scheduled_count else 0.0,
            )
        )
    return sorted(summaries, key=lambda item: item.slot_label)

def build_missed_dose_reply_context(
    session: Session,
    start_dt: datetime,
    end_dt: datetime,
    limit: int = 20,
) -> list[MissedDoseReplyContext]:
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.patient_id == settings.patient_id,
            Notification.notification_type == "conversation_alert",
            Notification.visible_at >= start_dt,
            Notification.visible_at <= end_dt,
        )
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
        .limit(limit)
    ).all()
    contexts: list[MissedDoseReplyContext] = []
    for notification in reversed(rows):
        metadata = parse_metadata_json(notification.metadata_json)
        if metadata.get("category") != "missed_dose" or not metadata.get("patient_reply"):
            continue
        event = session.get(DoseEvent, notification.related_dose_event_id) if notification.related_dose_event_id else None
        contexts.append(
            MissedDoseReplyContext(
                dose_event_id=notification.related_dose_event_id,
                medication_name=event.medication_name if event is not None else "",
                slot_label=event.slot_label if event is not None else "",
                scheduled_for=event.scheduled_for if event is not None else None,
                patient_reply=str(metadata.get("patient_reply") or ""),
                agent_reply=str(metadata.get("agent_reply") or ""),
                status=str(metadata.get("status") or ""),
                reply_understanding=metadata.get("missed_dose_reply_understanding")
                if isinstance(metadata.get("missed_dose_reply_understanding"), dict)
                else {},
                created_at=notification.created_at,
            )
        )
    return contexts

def build_conversation_context(
    session: Session,
    start_dt: datetime,
    end_dt: datetime,
    limit: int = 20,
) -> list[ChatTurn]:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.patient_id == settings.patient_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    ).all()
    return [
        ChatTurn(
            role=row.role if row.role in {"user", "assistant", "system"} else "user",
            content=row.content,
            created_at=row.created_at,
        )
        for row in reversed(rows)
    ]

def get_recent_slot_summaries(session: Session, lookback_days: int = 7) -> list[SlotAdherenceSummary]:
    clock = ensure_clock(session)
    start_dt = datetime.combine(clock.current_time.date() - timedelta(days=lookback_days), time.min)
    events = session.scalars(
        select(DoseEvent).where(
            DoseEvent.patient_id == settings.patient_id,
            DoseEvent.scheduled_for >= start_dt,
        )
    ).all()
    return build_slot_summaries(events)

def collect_missed_dose_payloads(session: Session, up_to_time: datetime) -> list[MissedDoseEventPayload]:
    stmt = select(DoseEvent).where(
        DoseEvent.patient_id == settings.patient_id,
        DoseEvent.status == "scheduled",
        DoseEvent.missed_handled.is_(False),
    )
    payloads: list[MissedDoseEventPayload] = []
    reminder_suppressed = is_reminder_suppressed_after_side_effect(session)
    missed_dose_conversation_enabled = ui_policy_enabled(
        session,
        MISSED_DOSE_CONVERSATION,
        patient_id=settings.patient_id,
    )

    for event in session.scalars(stmt).all():
        missed_due_at = missed_dose_due_at_for_event(session, event)
        if missed_due_at > up_to_time:
            continue
        event.status = "missed"
        event.missed_detected_at = missed_due_at
        event.missed_handled = True
        activate_missed_dose_flag(session, event, activated_at=missed_due_at)
        if reminder_suppressed or not missed_dose_conversation_enabled:
            continue
        notification = create_missed_dose_conversation_alert(session, event, missed_due_at)
        adherence_pattern_context, tone_policy_context = missed_dose_hybrid_context(session, event)
        payloads.append(
            MissedDoseEventPayload(
                patient_id=event.patient_id,
                dose_event_id=event.id,
                medication_name=event.medication_name,
                slot_label=event.slot_label,
                scheduled_for=event.scheduled_for,
                detected_at=missed_due_at,
                policy_context=resolve_policy_for_event(session, event),
                policy_boundary=resolve_policy_boundary_for_event(session, event),
                recent_slot_summaries=get_recent_slot_summaries(session),
                adherence_pattern_context=adherence_pattern_context,
                tone_policy_context=tone_policy_context,
                chat_context=[],
                missed_dose_reply_context=build_missed_dose_reply_context(
                    session,
                    event.scheduled_for - timedelta(days=7),
                    missed_due_at,
                ),
                conversation_context=build_conversation_context(
                    session,
                    event.scheduled_for - timedelta(days=7),
                    missed_due_at,
                ),
                callback_context=AgentCallbackContext(
                    app_base_url=settings.system_base_url,
                    notification_id=notification.id,
                ),
            )
        )
    session.flush()
    return payloads


def missed_dose_hybrid_context(session: Session, event: DoseEvent) -> tuple[dict, dict]:
    from system_app.services.adherence_pattern_service import evaluate_adherence_pattern
    from system_app.services.tone_policy_service import select_tone_policy

    pattern_decision = evaluate_adherence_pattern(session, event)
    tone_decision = select_tone_policy(session, event, pattern_decision.pattern_code)
    return pattern_decision.to_metadata(), tone_decision.to_metadata()

def prepare_notification_window(
    session: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[MissedDoseEventPayload]:
    if end_dt <= start_dt:
        clock = ensure_clock(session)
        clock.last_processed_sim_time = end_dt
        session.commit()
        return []

    event_generation_end_dt = end_dt + timedelta(minutes=max_primary_reminder_lead_minutes())
    generate_due_dose_events(session, start_dt, event_generation_end_dt)
    first_missed_due_at = find_first_missed_dose_due(session, end_dt)
    # Daily-pattern analysis scheduling remains outside this dose write path.
    # The Backend simulation worker must neither build nor dispatch a daily
    # pattern payload. It only advances medication/missed-dose state here.
    processing_end_dt = first_missed_due_at or end_dt
    generate_notifications_for_new_events(session, processing_end_dt)
    missed_payloads = collect_missed_dose_payloads(session, processing_end_dt)

    clock = ensure_clock(session)
    if missed_payloads and first_missed_due_at is not None:
        clock.current_time = first_missed_due_at
    clock.last_processed_sim_time = processing_end_dt
    session.commit()
    return missed_payloads

async def set_clock_state(
    session: Session,
    advance_minutes: int | None = None,
    is_running: bool | None = None,
    speed_multiplier: int | None = None,
    *,
    commit: bool = True,
) -> SimulationClock:
    clock = ensure_clock(session)

    if advance_minutes:
        clock.current_time = clock.current_time + timedelta(minutes=advance_minutes)
        ensure_day_events(session, clock.current_time.date())
        session.flush()

    if is_running is not None:
        clock.is_running = is_running
    if speed_multiplier is not None:
        clock.speed_multiplier = speed_multiplier
    clock.last_tick_real_at = utc_now()

    if commit:
        session.commit()
        session.refresh(clock)
    else:
        session.flush()
    return clock

def mark_dose_taken_command(session: Session, dose_event_id: int, taken_at: datetime | None = None) -> DoseEvent | None:
    event = session.get(DoseEvent, dose_event_id)
    if event is None:
        return None
    if event.status == "taken" and event.taken_at is not None:
        return event
    event.taken_at = taken_at or ensure_clock(session).current_time
    if event.status == "missed":
        event.note = "late_taken_after_miss"
    event.status = "taken"
    clear_missed_dose_flag_after_taken(session, event, taken_at=event.taken_at)
    session.flush()
    return event


def mark_dose_taken(session: Session, dose_event_id: int, taken_at: datetime | None = None) -> DoseEvent | None:
    event = mark_dose_taken_command(session, dose_event_id, taken_at=taken_at)
    if event is None:
        return None
    session.commit()
    session.refresh(event)
    return event
