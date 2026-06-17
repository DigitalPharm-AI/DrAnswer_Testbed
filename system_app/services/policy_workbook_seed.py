from __future__ import annotations

from typing import Any

from shared.settings import get_settings


def default_policy_row() -> list[Any]:
    settings = get_settings()
    extra_reminders = settings.policy_default_extra_reminders
    interval_minutes = settings.policy_default_interval_minutes
    missed_dose_after_minutes = max(
        settings.missed_dose_grace_minutes,
        (extra_reminders + 1) * interval_minutes,
    )
    return [
        "default_policy",
        "*",
        extra_reminders,
        interval_minutes,
        missed_dose_after_minutes,
        "at",
        0,
        "{medication_name} 복약 알림",
        "{slot_label} 복약 시간입니다. 복약 후 기록 버튼을 눌러주세요.",
        "{medication_name} 추가 복약 알림",
        "{slot_label} 복약을 놓치지 않도록 다시 알려드려요.",
        "미복용 AI 알림",
        "{slot_label} {medication_name} 미복용이 확정되어 AI가 상황을 확인하고 있어요.",
        True,
        0,
    ]


def default_boundary_row(policy_key: str) -> list[Any]:
    settings = get_settings()
    return [
        f"{policy_key}_boundary",
        policy_key,
        0,
        settings.policy_max_extra_reminders,
        settings.policy_min_interval_minutes,
        settings.policy_max_interval_minutes,
        settings.policy_min_missed_dose_after_minutes,
        settings.policy_max_missed_dose_after_minutes,
        "before,at,after",
        0,
        settings.policy_max_primary_reminder_offset_minutes,
        True,
        0,
    ]


def default_system_policy_row() -> list[Any]:
    return [
        "daily_pattern_conversation_time",
        "08:30",
        True,
        0,
        "일일 복약 패턴 대화 요청 시각",
    ]
