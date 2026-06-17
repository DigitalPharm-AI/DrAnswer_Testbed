from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import NotificationPolicyDelta
from shared.settings import get_settings
from system_app.services.policy_confirmation_constants import POLICY_ACTION_DECREASE, POLICY_ACTION_INCREASE, POLICY_ACTION_KEEP
from system_app.services.policy_service import (
    POLICY_TEMPLATE_FIELDS,
    completed_policy_values,
    relative_primary_reminder_minutes,
    resolve_policy_for_slot,
)

settings = get_settings()


def policy_delta_direction(session: Session, delta: NotificationPolicyDelta) -> str:
    current_policy = resolve_policy_for_slot(session, settings.patient_id, delta.slot_label, delta.effective_start_date)
    current_extra_reminders = current_policy.extra_reminders
    current_interval_minutes = current_policy.interval_minutes
    current_missed_dose_after_minutes = current_policy.missed_dose_after_minutes
    next_missed_dose_after_minutes = delta.missed_dose_after_minutes or current_missed_dose_after_minutes
    next_primary_timing = delta.primary_reminder_timing or current_policy.primary_reminder_timing
    next_primary_offset = (
        delta.primary_reminder_offset_minutes
        if delta.primary_reminder_offset_minutes is not None
        else current_policy.primary_reminder_offset_minutes
    )
    current_primary_minutes = relative_primary_reminder_minutes(
        current_policy.primary_reminder_timing,
        current_policy.primary_reminder_offset_minutes,
    )
    next_primary_minutes = relative_primary_reminder_minutes(next_primary_timing, next_primary_offset)
    signals: set[str] = set()
    if delta.extra_reminders > current_extra_reminders:
        signals.add(POLICY_ACTION_INCREASE)
    elif delta.extra_reminders < current_extra_reminders:
        signals.add(POLICY_ACTION_DECREASE)
    if delta.interval_minutes < current_interval_minutes:
        signals.add(POLICY_ACTION_INCREASE)
    elif delta.interval_minutes > current_interval_minutes:
        signals.add(POLICY_ACTION_DECREASE)
    if next_missed_dose_after_minutes < current_missed_dose_after_minutes:
        signals.add(POLICY_ACTION_INCREASE)
    elif next_missed_dose_after_minutes > current_missed_dose_after_minutes:
        signals.add(POLICY_ACTION_DECREASE)
    if next_primary_minutes < current_primary_minutes:
        signals.add(POLICY_ACTION_INCREASE)
    elif next_primary_minutes > current_primary_minutes:
        signals.add(POLICY_ACTION_DECREASE)
    if len(signals) == 1:
        return next(iter(signals))
    if not signals:
        return POLICY_ACTION_KEEP
    return "ambiguous"


def policy_delta_changed_fields(session: Session, delta: NotificationPolicyDelta) -> list[str]:
    current_policy = resolve_policy_for_slot(session, settings.patient_id, delta.slot_label, delta.effective_start_date)
    current_values = {
        "extra_reminders": current_policy.extra_reminders,
        "interval_minutes": current_policy.interval_minutes,
        "missed_dose_after_minutes": current_policy.missed_dose_after_minutes,
        "primary_reminder_timing": current_policy.primary_reminder_timing,
        "primary_reminder_offset_minutes": current_policy.primary_reminder_offset_minutes,
        **{field_name: getattr(current_policy, field_name) for field_name in POLICY_TEMPLATE_FIELDS},
    }
    proposed_values = {
        **completed_policy_values(delta, current_policy),
        **{
            field_name: getattr(delta, field_name) or getattr(current_policy, field_name)
            for field_name in POLICY_TEMPLATE_FIELDS
        },
    }
    return sorted(
        field_name
        for field_name, proposed_value in proposed_values.items()
        if current_values.get(field_name) != proposed_value
    )


def actionable_policy_deltas(session: Session, deltas: list[NotificationPolicyDelta]) -> list[NotificationPolicyDelta]:
    return [delta for delta in deltas if policy_delta_changed_fields(session, delta)]


def recommended_policy_action(session: Session, deltas: list[NotificationPolicyDelta]) -> str | None:
    directions = {policy_delta_direction(session, delta) for delta in deltas}
    if directions == {POLICY_ACTION_INCREASE}:
        return POLICY_ACTION_INCREASE
    if directions == {POLICY_ACTION_DECREASE}:
        return POLICY_ACTION_DECREASE
    if directions == {POLICY_ACTION_KEEP}:
        return POLICY_ACTION_KEEP
    return None


def policy_confirmation_actions(recommended_action: str | None) -> list[str]:
    if recommended_action == POLICY_ACTION_INCREASE:
        return [POLICY_ACTION_INCREASE, POLICY_ACTION_KEEP]
    if recommended_action == POLICY_ACTION_DECREASE:
        return [POLICY_ACTION_DECREASE, POLICY_ACTION_KEEP]
    if recommended_action == POLICY_ACTION_KEEP:
        return [POLICY_ACTION_KEEP]
    return [POLICY_ACTION_INCREASE, POLICY_ACTION_DECREASE, POLICY_ACTION_KEEP]


def adjusted_policy_delta_for_confirmation(
    delta: NotificationPolicyDelta,
    action: str,
    *,
    recommended_action: str | None,
) -> NotificationPolicyDelta:
    if action != POLICY_ACTION_DECREASE or recommended_action == POLICY_ACTION_DECREASE:
        return delta
    payload = delta.model_dump(mode="json")
    payload["extra_reminders"] = max(delta.extra_reminders - 1, 0)
    payload["reason"] = f"{delta.reason} / 사용자 확인: 줄이기 선택"
    return NotificationPolicyDelta.model_validate(payload)


def selected_policy_deltas_for_confirmation(
    session: Session,
    deltas: list[NotificationPolicyDelta],
    action: str,
    *,
    recommended_action: str | None,
) -> list[NotificationPolicyDelta]:
    if recommended_action is None and action in {POLICY_ACTION_INCREASE, POLICY_ACTION_DECREASE}:
        matching_deltas = [delta for delta in deltas if policy_delta_direction(session, delta) == action]
        if matching_deltas:
            return matching_deltas
    return [
        adjusted_policy_delta_for_confirmation(delta, action, recommended_action=recommended_action)
        for delta in deltas
    ]
