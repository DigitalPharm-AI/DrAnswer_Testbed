from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import NotificationPolicyDelta
from shared.settings import get_settings
from system_app.services.policy_confirmation_diff import policy_delta_changed_fields
from system_app.services.policy_service import completed_policy_values, resolve_policy_for_slot

settings = get_settings()


def primary_timing_text(timing: str | None, offset: int | None) -> str:
    if timing is None or offset is None:
        return "현재 정책 유지"
    if timing == "at":
        return "정시 알림"
    if timing == "before":
        return f"복약 {offset}분 전 알림"
    return f"복약 {offset}분 후 알림"


def primary_timing_label(delta: NotificationPolicyDelta) -> str:
    timing_text = primary_timing_text(delta.primary_reminder_timing, delta.primary_reminder_offset_minutes)
    return "" if timing_text == "현재 정책 유지" else f", {timing_text}"


def policy_delta_confirmation_line(delta: NotificationPolicyDelta) -> str:
    missed_dose_line = f", 미복용 판단 {delta.missed_dose_after_minutes}분 후" if delta.missed_dose_after_minutes is not None else ""
    return f"{delta.slot_label}: 추가 {delta.extra_reminders}회, {delta.interval_minutes}분 간격{missed_dose_line}{primary_timing_label(delta)}"


def policy_confirmation_body(deltas: list[NotificationPolicyDelta], multiple_choice: dict) -> str:
    return str(multiple_choice.get("question") or "알림 설정을 변경하시는 건 어떨까요?")


def policy_values_display(values: dict) -> dict:
    return {
        "extra_reminders": values["extra_reminders"],
        "interval_minutes": values["interval_minutes"],
        "missed_dose_after_minutes": values["missed_dose_after_minutes"],
        "primary_reminder_timing": values["primary_reminder_timing"],
        "primary_reminder_offset_minutes": values["primary_reminder_offset_minutes"],
        "extra_reminders_label": f"추가 {values['extra_reminders']}회",
        "interval_minutes_label": f"{values['interval_minutes']}분 간격",
        "missed_dose_after_minutes_label": f"{values['missed_dose_after_minutes']}분 후",
        "primary_reminder_label": primary_timing_text(
            values["primary_reminder_timing"],
            values["primary_reminder_offset_minutes"],
        ),
        "summary": (
            f"추가 {values['extra_reminders']}회 · "
            f"{values['interval_minutes']}분 간격 · "
            f"미복용 {values['missed_dose_after_minutes']}분 후 · "
            f"{primary_timing_text(values['primary_reminder_timing'], values['primary_reminder_offset_minutes'])}"
        ),
    }


def policy_confirmation_candidate(session: Session, delta: NotificationPolicyDelta) -> dict:
    current_policy = resolve_policy_for_slot(session, settings.patient_id, delta.slot_label, delta.effective_start_date)
    current_values = {
        "extra_reminders": current_policy.extra_reminders,
        "interval_minutes": current_policy.interval_minutes,
        "missed_dose_after_minutes": current_policy.missed_dose_after_minutes,
        "primary_reminder_timing": current_policy.primary_reminder_timing,
        "primary_reminder_offset_minutes": current_policy.primary_reminder_offset_minutes,
    }
    proposed_values = completed_policy_values(delta, current_policy)
    return {
        "slot_label": delta.slot_label,
        "current": policy_values_display(current_values),
        "proposed": policy_values_display(proposed_values),
        "changed_fields": policy_delta_changed_fields(session, delta),
        "reason": delta.reason,
        "raw_delta": delta.model_dump(mode="json"),
        "summary": policy_delta_confirmation_line(delta),
    }


def policy_change_payload(session: Session, deltas: list[NotificationPolicyDelta], multiple_choice: dict) -> dict:
    return {
        "question": policy_confirmation_body(deltas, multiple_choice),
        "candidates": [policy_confirmation_candidate(session, delta) for delta in deltas],
        "options": multiple_choice.get("options", []),
        "recommended_action": multiple_choice.get("recommended_value"),
    }
