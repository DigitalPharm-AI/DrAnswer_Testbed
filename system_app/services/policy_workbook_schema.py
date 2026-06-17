from __future__ import annotations

import re

POLICY_SHEET_NAME = "notification_policies"
BOUNDARY_SHEET_NAME = "policy_boundaries"
SYSTEM_POLICY_SHEET_NAME = "system_policies"
SHEET_NAME = POLICY_SHEET_NAME
DAILY_PATTERN_CONVERSATION_TIME_KEY = "daily_pattern_conversation_time"
LEGACY_DAILY_PATTERN_ANALYSIS_TIME_KEY = "daily_pattern_analysis_time"
DEFAULT_DAILY_PATTERN_CONVERSATION_TIME = "08:30"

POLICY_REQUIRED_COLUMNS = [
    "policy_key",
    "slot_label",
    "extra_reminders",
    "interval_minutes",
    "missed_dose_after_minutes",
    "primary_reminder_timing",
    "primary_reminder_offset_minutes",
    "medication_title_template",
    "medication_body_template",
    "extra_title_template",
    "extra_body_template",
    "missed_dose_title_template",
    "missed_dose_body_template",
    "active",
    "priority",
]
REQUIRED_COLUMNS = POLICY_REQUIRED_COLUMNS

BOUNDARY_REQUIRED_COLUMNS = [
    "boundary_key",
    "policy_key",
    "min_extra_reminders",
    "max_extra_reminders",
    "min_interval_minutes",
    "max_interval_minutes",
    "min_missed_dose_after_minutes",
    "max_missed_dose_after_minutes",
    "allowed_primary_reminder_timings",
    "min_primary_reminder_offset_minutes",
    "max_primary_reminder_offset_minutes",
    "active",
    "priority",
]

SYSTEM_POLICY_REQUIRED_COLUMNS = [
    "policy_key",
    "value",
    "active",
    "priority",
    "description",
]

TEMPLATE_COLUMNS = [
    "medication_title_template",
    "medication_body_template",
    "extra_title_template",
    "extra_body_template",
    "missed_dose_title_template",
    "missed_dose_body_template",
]
ALLOWED_TEMPLATE_PLACEHOLDERS = {"medication_name", "slot_label", "scheduled_time"}
ALLOWED_PRIMARY_REMINDER_TIMINGS = {"before", "at", "after"}
POLICY_COLUMN_DEFAULTS = {
    "primary_reminder_timing": "at",
    "primary_reminder_offset_minutes": 0,
}
PLACEHOLDER_PATTERN = re.compile(r"{([^{}]+)}")


def relative_primary_minutes(timing: str, offset_minutes: int) -> int:
    if timing == "before":
        return -offset_minutes
    if timing == "after":
        return offset_minutes
    return 0
