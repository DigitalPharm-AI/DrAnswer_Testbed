from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import DoseEvent, MissedDoseFlag, Notification
from system_app.services.ui_time import as_simulation_naive_datetime

settings = get_settings()


def activate_missed_dose_flag(session: Session, event: DoseEvent, *, activated_at: datetime | None = None) -> MissedDoseFlag:
    flag_date = as_simulation_naive_datetime(event.scheduled_for).date()
    activated = as_simulation_naive_datetime(
        activated_at or event.missed_detected_at or event.scheduled_for
    )
    flag = _flag_for_date(session, event.patient_id, flag_date)
    if flag is None:
        flag = MissedDoseFlag(
            patient_id=event.patient_id,
            flag_date=flag_date,
            active=True,
            trigger_slot_label=event.slot_label,
            related_dose_event_id=event.id,
            activated_at=activated,
            cleared_at=None,
            clear_reason="",
            subsequent_taken_count=0,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        session.add(flag)
    else:
        flag.active = True
        flag.trigger_slot_label = event.slot_label
        flag.related_dose_event_id = event.id
        flag.activated_at = activated
        flag.cleared_at = None
        flag.clear_reason = ""
        flag.subsequent_taken_count = 0
        flag.updated_at = utc_now()
    session.flush()
    return flag


def clear_missed_dose_flag_after_taken(session: Session, event: DoseEvent, *, taken_at: datetime | None = None) -> MissedDoseFlag | None:
    scheduled_for = as_simulation_naive_datetime(event.scheduled_for)
    flag = _flag_for_date(session, event.patient_id, scheduled_for.date())
    if flag is None or not flag.active:
        return flag
    observed_at = as_simulation_naive_datetime(
        taken_at or event.taken_at or utc_now()
    )
    activated_at = as_simulation_naive_datetime(flag.activated_at)
    if flag.related_dose_event_id == event.id:
        return _clear_flag(session, flag, observed_at, "missed_event_marked_taken")
    if observed_at < activated_at and scheduled_for < activated_at:
        return flag
    flag.subsequent_taken_count += 1
    return _clear_flag(session, flag, observed_at, "subsequent_same_day_taken")


def is_active_missed_dose_flag_for_event(session: Session, dose_event_id: int | None) -> bool:
    if dose_event_id is None:
        return False
    event = session.get(DoseEvent, dose_event_id)
    if event is None:
        return False
    flag = _flag_for_date(session, event.patient_id, event.scheduled_for.date())
    return bool(flag and flag.active and flag.related_dose_event_id == event.id)


def active_missed_dose_flag_for_date(session: Session, flag_date, patient_id: str | None = None) -> MissedDoseFlag | None:
    flag = _flag_for_date(session, patient_id or settings.patient_id, flag_date)
    return flag if flag is not None and flag.active else None


def _flag_for_date(session: Session, patient_id: str, flag_date) -> MissedDoseFlag | None:
    return session.scalar(
        select(MissedDoseFlag).where(
            MissedDoseFlag.patient_id == patient_id,
            MissedDoseFlag.flag_date == flag_date,
        )
    )


def _clear_flag(session: Session, flag: MissedDoseFlag, cleared_at: datetime, reason: str) -> MissedDoseFlag:
    cleared_at = as_simulation_naive_datetime(cleared_at)
    flag.active = False
    flag.cleared_at = cleared_at
    flag.clear_reason = reason
    flag.updated_at = utc_now()
    if flag.related_dose_event_id is not None:
        prompts = session.scalars(
            select(Notification).where(
                Notification.notification_type == "conversation_alert",
                Notification.related_dose_event_id == flag.related_dose_event_id,
                Notification.acknowledged.is_(False),
            )
        ).all()
        for prompt in prompts:
            metadata = parse_json_object(prompt.metadata_json)
            if metadata.get("category") != "missed_dose":
                continue
            metadata["status"] = "superseded"
            metadata["superseded_reason"] = reason
            metadata["resolved_at"] = cleared_at.isoformat()
            prompt.metadata_json = dump_json(metadata)
            prompt.acknowledged = True
    session.flush()
    return flag
