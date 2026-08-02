from __future__ import annotations

from typing import Any, Literal

NotificationPolicySource = Literal[
    "pattern_analysis",
    "patient_request",
    "system_request",
]
SystemPolicySource = Literal["patient_request", "system_request"]

_NOTIFICATION_POLICY_SOURCES = frozenset(
    {"pattern_analysis", "patient_request", "system_request"}
)
_SYSTEM_POLICY_SOURCES = frozenset({"patient_request", "system_request"})


def normalize_notification_policy_source(
    value: Any,
    *,
    source_event_type: str,
) -> NotificationPolicySource:
    if source_event_type in {"daily_pattern", "manual_daily_pattern"}:
        return "pattern_analysis"
    if source_event_type == "multiturn_chat":
        return "patient_request"
    if isinstance(value, str) and value in _NOTIFICATION_POLICY_SOURCES:
        return value
    return "system_request"


def normalize_system_policy_source(
    value: Any,
    *,
    source_event_type: str,
) -> SystemPolicySource:
    if isinstance(value, str) and value in _SYSTEM_POLICY_SOURCES:
        return value
    if source_event_type == "multiturn_chat":
        return "patient_request"
    return "system_request"
