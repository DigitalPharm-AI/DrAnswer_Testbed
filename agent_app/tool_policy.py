from __future__ import annotations

from typing import Any

from shared.schemas import NotificationPolicyDelta, ToolCallResult

DEFERRED_POLICY_TOOL_NAMES = {"apply_notification_policy", "apply_system_policy"}


def is_deferred_policy_tool_call(tool_call: dict[str, Any]) -> bool:
    return str(tool_call.get("name") or "") in DEFERRED_POLICY_TOOL_NAMES


def deferred_policy_tool_result(tool_call: dict[str, Any], *, trace_id: str, source_event_type: str) -> ToolCallResult:
    tool_name = str(tool_call.get("name") or "unknown")
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    slot_label = str(arguments.get("slot_label") or "").strip()
    policy_key = str(arguments.get("policy_key") or "").strip()
    response = {
        "tool_name": tool_name,
        "reason": "policy_confirmation_required",
    }
    if slot_label:
        response["slot_label"] = slot_label
    if policy_key:
        response["policy_key"] = policy_key
    return ToolCallResult(
        tool_name=tool_name,
        status="skipped",
        response=response,
        idempotency_key=f"{trace_id}:{tool_name}:{source_event_type}:confirmation",
    )


def has_notification_policy_tool_call(tool_calls: list[dict[str, Any]]) -> bool:
    return any(str(tool_call.get("name") or "") == "apply_notification_policy" for tool_call in tool_calls)


def has_deferred_policy_tool_call(tool_calls: list[dict[str, Any]]) -> bool:
    return any(str(tool_call.get("name") or "") in DEFERRED_POLICY_TOOL_NAMES for tool_call in tool_calls)


def normalize_policy_tool_calls(tool_calls: list[dict[str, Any]], *, source_event_type: str) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if str(tool_call.get("name") or "") != "apply_notification_policy":
            normalized.append(tool_call)
            continue
        arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
        policies = _notification_policy_deltas(arguments, source_event_type=source_event_type)
        for policy in policies:
            normalized.append(
                {
                    **tool_call,
                    "arguments": policy.model_dump(mode="json"),
                }
            )
    return normalized


def _notification_policy_deltas(arguments: dict[str, Any], *, source_event_type: str = "") -> list[NotificationPolicyDelta]:
    raw_items = arguments.get("policies")
    if raw_items is None:
        raw_items = arguments.get("policy_deltas")
    if raw_items is None and isinstance(arguments.get("policy_delta"), dict):
        raw_items = [arguments["policy_delta"]]
    if raw_items is None:
        raw_items = [arguments]
    if not isinstance(raw_items, list):
        raise ValueError("apply_notification_policy requires a policy object or policies list.")
    normalized_items = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        payload = dict(item)
        payload["source"] = _normalize_notification_policy_source(payload.get("source"), source_event_type)
        normalized_items.append(NotificationPolicyDelta.model_validate(payload))
    return normalized_items


def _normalize_notification_policy_source(value: Any, source_event_type: str) -> str:
    if source_event_type in {"daily_pattern", "manual_daily_pattern"}:
        return "pattern_analysis"
    if source_event_type == "multiturn_chat":
        return "patient_request"
    allowed = {"pattern_analysis", "patient_request", "system_request"}
    if isinstance(value, str) and value in allowed:
        return value
    return _default_notification_policy_source(source_event_type)


def _default_notification_policy_source(source_event_type: str) -> str:
    if source_event_type in {"daily_pattern", "manual_daily_pattern"}:
        return "pattern_analysis"
    if source_event_type == "multiturn_chat":
        return "patient_request"
    return "system_request"
