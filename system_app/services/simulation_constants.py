from __future__ import annotations

from datetime import datetime, time

CUSTOM_CHOICE = "__custom__"
MEDICATION_PRESET_OPTIONS = [
    "혈압약",
    "당뇨약",
    "고지혈증약",
    "비타민D",
    "진통제",
    "영양제",
    CUSTOM_CHOICE,
]
PHR_SYNC_UNREGISTERED = "unregistered"
PHR_SYNC_SYNCED = "synced"
PHR_SYNC_NEEDS_SYNC = "needs_sync"
PHR_SYNC_FAILED = "sync_failed"
DOSAGE_PRESET_OPTIONS = [
    "1정",
    "2정",
    "1캡슐",
    "10ml",
    CUSTOM_CHOICE,
]
SCHEDULE_TEMPLATE_MAP: dict[str, dict[str, object]] = {
    "morning_lunch_evening": {
        "label": "아침 / 점심 / 야간",
        "times": ["08:00", "13:00", "21:00"],
    },
    "morning_evening": {
        "label": "아침 / 야간",
        "times": ["08:00", "21:00"],
    },
}


def parse_times_csv(times_csv: str) -> list[str]:
    seen: list[str] = []
    for raw in times_csv.split(","):
        value = raw.strip()
        if not value:
            continue
        try:
            parsed = datetime.strptime(value, "%H:%M").strftime("%H:%M")
        except ValueError as exc:
            raise ValueError("invalid_schedule_time_format") from exc
        if parsed not in seen:
            seen.append(parsed)
    if not seen:
        raise ValueError("required_schedule_missing")
    return seen

def resolve_choice_value(choice: str | None, custom_value: str | None = None, fallback_value: str | None = None) -> str:
    if choice and choice != CUSTOM_CHOICE:
        return choice.strip()
    if custom_value and custom_value.strip():
        return custom_value.strip()
    if fallback_value and fallback_value.strip():
        return fallback_value.strip()
    raise ValueError("required_choice_missing")

def resolve_schedule_times(schedule_template: str | None = None, fallback_times_csv: str | None = None) -> str:
    if schedule_template and schedule_template in SCHEDULE_TEMPLATE_MAP:
        return ", ".join(SCHEDULE_TEMPLATE_MAP[schedule_template]["times"])
    if fallback_times_csv and fallback_times_csv.strip():
        return fallback_times_csv.strip()
    raise ValueError("required_schedule_missing")

def period_label(clock_time: time) -> str:
    hour = clock_time.hour
    if 5 <= hour < 11:
        return "아침"
    if 11 <= hour < 15:
        return "점심"
    if 15 <= hour < 21:
        return "저녁"
    return "야간"

def slot_label_for_time(scheduled_time: str) -> str:
    clock_time = datetime.strptime(scheduled_time, "%H:%M").time()
    return f"{period_label(clock_time)} {scheduled_time}"
