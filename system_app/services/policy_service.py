from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.schemas import NotificationPolicyDelta, PolicyWorkbookLoadResult, ResolvedNotificationPolicy, ResolvedPolicyBoundary, SystemPolicyDelta
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import DoseEvent, ReminderPolicy, SystemPolicyOverride
from system_app.services.policy_workbook import (
    ALLOWED_TEMPLATE_PLACEHOLDERS,
    PLACEHOLDER_PATTERN,
    policy_workbook_manager,
)
from system_app.services.policy_workbook_schema import (
    DAILY_PATTERN_CONVERSATION_TIME_KEY,
    DEFAULT_DAILY_PATTERN_CONVERSATION_TIME,
)

settings = get_settings()

POLICY_TEMPLATE_FIELDS = [
    "medication_title_template",
    "medication_body_template",
    "extra_title_template",
    "extra_body_template",
    "missed_dose_title_template",
    "missed_dose_body_template",
]


def validate_policy_template(value: str | None, field_name: str) -> tuple[bool, str]:
    if value is None:
        return True, "ok"
    if not value.strip():
        return False, f"{field_name}은 비어 있을 수 없습니다."
    unknown = sorted(set(PLACEHOLDER_PATTERN.findall(value)) - ALLOWED_TEMPLATE_PLACEHOLDERS)
    if unknown:
        return False, f"{field_name}에 허용되지 않은 placeholder가 있습니다: {', '.join(unknown)}"
    return True, "ok"


def validate_policy_delta(
    delta: NotificationPolicyDelta,
    *,
    session: Session | None = None,
    patient_id: str | None = None,
) -> tuple[bool, str]:
    if delta.effective_end_date < delta.effective_start_date:
        return False, "정책 종료일이 시작일보다 빠를 수 없습니다."
    for field_name in POLICY_TEMPLATE_FIELDS:
        is_valid, message = validate_policy_template(getattr(delta, field_name), field_name)
        if not is_valid:
            return False, message

    base = (
        resolve_policy_for_slot(session, patient_id or settings.patient_id, delta.slot_label, delta.effective_start_date)
        if session is not None
        else policy_workbook_manager.resolve_default(delta.slot_label)
    )
    boundary = resolve_policy_boundary_for_slot(session, patient_id or settings.patient_id, delta.slot_label, delta.effective_start_date)
    policy_values = completed_policy_values(delta, base)
    boundary_errors = policy_boundary_violations(policy_values, boundary)
    if boundary_errors:
        return False, boundary_errors[0]
    return True, "ok"


def reload_policy_workbook() -> PolicyWorkbookLoadResult:
    return policy_workbook_manager.reload()


def get_active_system_policy_override(
    session: Session,
    policy_key: str,
    *,
    patient_id: str | None = None,
) -> SystemPolicyOverride | None:
    stmt = (
        select(SystemPolicyOverride)
        .where(
            SystemPolicyOverride.patient_id == (patient_id or settings.patient_id),
            SystemPolicyOverride.policy_key == policy_key,
            SystemPolicyOverride.active.is_(True),
        )
        .order_by(desc(SystemPolicyOverride.updated_at), desc(SystemPolicyOverride.created_at), desc(SystemPolicyOverride.id))
    )
    return session.scalar(stmt)


def system_policy_value(
    session: Session | None,
    policy_key: str,
    default: str,
    *,
    patient_id: str | None = None,
) -> str:
    if session is not None:
        override = get_active_system_policy_override(session, policy_key, patient_id=patient_id)
        if override is not None:
            return override.value
    return policy_workbook_manager.system_policy_value(policy_key, default)


def daily_pattern_conversation_time(session: Session | None = None) -> time:
    value = policy_workbook_manager.system_policy_value(
        DAILY_PATTERN_CONVERSATION_TIME_KEY,
        DEFAULT_DAILY_PATTERN_CONVERSATION_TIME,
    )
    if session is not None:
        value = system_policy_value(
            session,
            DAILY_PATTERN_CONVERSATION_TIME_KEY,
            value,
        )
    return time.fromisoformat(value)


def daily_pattern_conversation_time_view(session: Session) -> dict[str, Any]:
    workbook_value = policy_workbook_manager.system_policy_value(
        DAILY_PATTERN_CONVERSATION_TIME_KEY,
        DEFAULT_DAILY_PATTERN_CONVERSATION_TIME,
    )
    override = get_active_system_policy_override(session, DAILY_PATTERN_CONVERSATION_TIME_KEY)
    value = override.value if override is not None else workbook_value
    return {
        "policy_key": DAILY_PATTERN_CONVERSATION_TIME_KEY,
        "label": "일일 패턴 대화 요청 시간",
        "value": value,
        "source_label": "요청 정책" if override is not None else "기본 정책",
        "reason": override.reason if override is not None else "system_policies 기본값",
        "updated_at_label": override.updated_at.strftime("%Y-%m-%d %H:%M") if override is not None and override.updated_at else "",
    }


def validate_system_policy_delta(delta: SystemPolicyDelta) -> tuple[bool, str]:
    if delta.policy_key != DAILY_PATTERN_CONVERSATION_TIME_KEY:
        return False, f"지원하지 않는 시스템 정책입니다: {delta.policy_key}"
    try:
        time.fromisoformat(delta.value)
    except ValueError:
        return False, "daily_pattern_conversation_time은 HH:MM 형식이어야 합니다."
    if len(delta.value) != 5:
        return False, "daily_pattern_conversation_time은 HH:MM 형식이어야 합니다."
    if not delta.reason.strip():
        return False, "시스템 정책 변경 근거가 필요합니다."
    return True, "ok"


def apply_system_policy_delta(session: Session, delta: SystemPolicyDelta) -> tuple[bool, str]:
    delta.value = delta.value.strip()
    delta.reason = delta.reason.strip()
    is_valid, message = validate_system_policy_delta(delta)
    if not is_valid:
        return False, message

    with session.begin_nested():
        active_rows = session.scalars(
            select(SystemPolicyOverride).where(
                SystemPolicyOverride.patient_id == settings.patient_id,
                SystemPolicyOverride.policy_key == delta.policy_key,
                SystemPolicyOverride.active.is_(True),
            )
        ).all()
        for row in active_rows:
            row.active = False
            row.updated_at = utc_now()

        session.add(
            SystemPolicyOverride(
                patient_id=settings.patient_id,
                policy_key=delta.policy_key,
                value=delta.value,
                reason=delta.reason,
                source=delta.source,
                active=True,
            )
        )
    session.flush()
    return True, f"일일 패턴 대화 요청 시간을 {delta.value}로 변경했습니다."


def get_applicable_policy(
    session: Session,
    slot_label: str,
    target_date: date,
    *,
    patient_id: str | None = None,
) -> ReminderPolicy | None:
    stmt = (
        select(ReminderPolicy)
        .where(
            ReminderPolicy.patient_id == (patient_id or settings.patient_id),
            ReminderPolicy.slot_label == slot_label,
            ReminderPolicy.active.is_(True),
            ReminderPolicy.effective_start_date <= target_date,
            ReminderPolicy.effective_end_date >= target_date,
        )
        .order_by(desc(ReminderPolicy.updated_at), desc(ReminderPolicy.created_at), desc(ReminderPolicy.id))
    )
    return session.scalar(stmt)


def resolved_policy_from_override(row: ReminderPolicy, base: ResolvedNotificationPolicy) -> ResolvedNotificationPolicy:
    payload = base.model_dump()
    payload.update(
        {
            "policy_key": row.policy_key or base.policy_key,
            "patient_id": row.patient_id,
            "slot_label": row.slot_label,
            "extra_reminders": row.extra_reminders,
            "interval_minutes": row.interval_minutes,
            "missed_dose_after_minutes": (
                row.missed_dose_after_minutes
                if row.missed_dose_after_minutes is not None
                else policy_missed_dose_delay_minutes(row.extra_reminders, row.interval_minutes)
            ),
            "primary_reminder_timing": getattr(row, "primary_reminder_timing", None) or base.primary_reminder_timing,
            "primary_reminder_offset_minutes": (
                getattr(row, "primary_reminder_offset_minutes", None)
                if getattr(row, "primary_reminder_offset_minutes", None) is not None
                else base.primary_reminder_offset_minutes
            ),
            "source": "patient_override",
            "reason": row.reason,
            "priority": base.priority,
        }
    )
    for field_name in POLICY_TEMPLATE_FIELDS:
        payload[field_name] = getattr(row, field_name, None) or getattr(base, field_name)
    return ResolvedNotificationPolicy.model_validate(payload)


def resolve_policy_for_slot(
    session: Session,
    patient_id: str,
    slot_label: str,
    target_date: date,
) -> ResolvedNotificationPolicy:
    base = policy_workbook_manager.resolve_default(slot_label)
    override = get_applicable_policy(session, slot_label, target_date, patient_id=patient_id)
    if override is None:
        return base
    return resolved_policy_from_override(override, base)


def resolve_policy_boundary_for_slot(
    session: Session | None,
    patient_id: str,
    slot_label: str,
    target_date: date,
) -> ResolvedPolicyBoundary:
    base_policy = policy_workbook_manager.resolve_default(slot_label)
    if session is None:
        policy = base_policy
    else:
        policy = resolve_policy_for_slot(session, patient_id, slot_label, target_date)
    return policy_workbook_manager.resolve_boundary(
        policy.policy_key,
        requested_slot_label=slot_label,
        fallback_policy_key=base_policy.policy_key,
    )


def resolve_policy_for_event(session: Session, event: DoseEvent) -> ResolvedNotificationPolicy:
    return resolve_policy_for_slot(session, event.patient_id, event.slot_label, event.scheduled_for.date())


def resolve_policy_boundary_for_event(session: Session, event: DoseEvent) -> ResolvedPolicyBoundary:
    return resolve_policy_boundary_for_slot(session, event.patient_id, event.slot_label, event.scheduled_for.date())


def render_policy_template(template: str, *, medication_name: str, slot_label: str, scheduled_for: datetime) -> str:
    values = {
        "medication_name": medication_name,
        "slot_label": slot_label,
        "scheduled_time": scheduled_for.strftime("%H:%M"),
    }
    return template.format_map(values)


def completed_policy_values(delta: NotificationPolicyDelta, base: ResolvedNotificationPolicy) -> dict[str, Any]:
    return {
        "extra_reminders": delta.extra_reminders,
        "interval_minutes": delta.interval_minutes,
        "missed_dose_after_minutes": delta.missed_dose_after_minutes or base.missed_dose_after_minutes,
        "primary_reminder_timing": delta.primary_reminder_timing or base.primary_reminder_timing,
        "primary_reminder_offset_minutes": (
            delta.primary_reminder_offset_minutes
            if delta.primary_reminder_offset_minutes is not None
            else base.primary_reminder_offset_minutes
        ),
    }


def policy_boundary_violations(values: dict[str, Any], boundary: ResolvedPolicyBoundary) -> list[str]:
    errors: list[str] = []
    extra_reminders = int(values["extra_reminders"])
    interval_minutes = int(values["interval_minutes"])
    missed_dose_after_minutes = int(values["missed_dose_after_minutes"])
    primary_reminder_timing = str(values["primary_reminder_timing"])
    primary_reminder_offset_minutes = int(values["primary_reminder_offset_minutes"])

    if not boundary.min_extra_reminders <= extra_reminders <= boundary.max_extra_reminders:
        errors.append(f"추가 알림은 {boundary.min_extra_reminders}~{boundary.max_extra_reminders}회 범위에서만 설정할 수 있습니다.")
    if not boundary.min_interval_minutes <= interval_minutes <= boundary.max_interval_minutes:
        errors.append(f"알림 간격은 {boundary.min_interval_minutes}~{boundary.max_interval_minutes}분 범위에서만 설정할 수 있습니다.")
    if not boundary.min_missed_dose_after_minutes <= missed_dose_after_minutes <= boundary.max_missed_dose_after_minutes:
        errors.append(
            f"미복용 AI 알림은 복약 예정 후 {boundary.min_missed_dose_after_minutes}~{boundary.max_missed_dose_after_minutes}분 범위에서만 설정할 수 있습니다."
        )
    if primary_reminder_timing not in boundary.allowed_primary_reminder_timings:
        allowed = ", ".join(boundary.allowed_primary_reminder_timings)
        errors.append(f"복약 알림 시점은 {allowed} 중 하나여야 합니다.")
    if not boundary.min_primary_reminder_offset_minutes <= primary_reminder_offset_minutes <= boundary.max_primary_reminder_offset_minutes:
        errors.append(
            f"복약 알림 offset은 {boundary.min_primary_reminder_offset_minutes}~{boundary.max_primary_reminder_offset_minutes}분 범위에서만 설정할 수 있습니다."
        )
    if primary_reminder_timing == "at" and primary_reminder_offset_minutes != 0:
        errors.append("정시(at) 알림은 offset을 0분으로 설정해야 합니다.")
    last_alert_minutes = relative_primary_reminder_minutes(primary_reminder_timing, primary_reminder_offset_minutes) + (
        extra_reminders * interval_minutes
    )
    if last_alert_minutes > missed_dose_after_minutes:
        errors.append("마지막 복약 알림 시각은 미복용 AI 알림 시각보다 늦을 수 없습니다.")
    return errors


def policy_missed_dose_delay_minutes(extra_reminders: int, interval_minutes: int) -> int:
    return max(settings.policy_min_interval_minutes, interval_minutes) * (extra_reminders + 1)


def missed_dose_delay_minutes_for_event(session: Session, event: DoseEvent) -> int:
    return resolve_policy_for_event(session, event).missed_dose_after_minutes


def missed_dose_due_at_for_event(session: Session, event: DoseEvent) -> datetime:
    return event.scheduled_for + timedelta(minutes=missed_dose_delay_minutes_for_event(session, event))


def relative_primary_reminder_minutes(timing: str, offset_minutes: int) -> int:
    if timing == "before":
        return -offset_minutes
    if timing == "after":
        return offset_minutes
    return 0


def primary_reminder_visible_at(policy: ResolvedNotificationPolicy, scheduled_for: datetime) -> datetime:
    return scheduled_for + timedelta(
        minutes=relative_primary_reminder_minutes(policy.primary_reminder_timing, policy.primary_reminder_offset_minutes)
    )


def extra_reminder_visible_at(policy: ResolvedNotificationPolicy, scheduled_for: datetime, index: int) -> datetime:
    return primary_reminder_visible_at(policy, scheduled_for) + timedelta(minutes=policy.interval_minutes * (index + 1))


def max_primary_reminder_lead_minutes() -> int:
    return policy_workbook_manager.max_primary_reminder_lead_minutes()
