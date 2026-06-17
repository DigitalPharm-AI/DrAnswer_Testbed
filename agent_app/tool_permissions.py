from __future__ import annotations

from typing import Any

from shared.schemas import ToolCallResult

SIDE_EFFECT_TOOLS = {"lookup_side_effect_info", "AE_pro_ctcae"}
POLICY_TOOLS = {"apply_notification_policy", "apply_system_policy"}

TOOL_ALLOWLIST: dict[str, set[str]] = {
    "daily_pattern": {"apply_notification_policy"},
    "manual_daily_pattern": {"apply_notification_policy"},
    "missed_dose": SIDE_EFFECT_TOOLS,
    "multiturn_chat": {"mark_dose_taken", *SIDE_EFFECT_TOOLS, *POLICY_TOOLS},
    "mcp": {"AE_pro_ctcae"},
}


def permission_denied_result(
    tool_call: dict[str, Any],
    *,
    trace_id: str,
    source_event_type: str,
    reason: str,
) -> ToolCallResult:
    tool_name = str(tool_call.get("name") or "unknown")
    return ToolCallResult(
        tool_name=tool_name,
        status="error",
        error="tool_permission_denied",
        response={
            "reason": reason,
            "source_event_type": source_event_type,
        },
        idempotency_key=f"{trace_id}:{tool_name}:{source_event_type}:permission_denied",
    )


def validate_tool_permission(tool_call: dict[str, Any], *, source_event_type: str, payload: dict[str, Any]) -> str | None:
    tool_name = str(tool_call.get("name") or "")
    allowed_tools = TOOL_ALLOWLIST.get(source_event_type, TOOL_ALLOWLIST.get("mcp", set()))
    if tool_name not in allowed_tools:
        return f"{tool_name or 'unknown'} is not allowed for {source_event_type}"
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    if tool_name == "mark_dose_taken":
        return _validate_mark_dose_taken(arguments, source_event_type=source_event_type, payload=payload)
    if tool_name in POLICY_TOOLS and source_event_type not in {"daily_pattern", "manual_daily_pattern", "multiturn_chat"}:
        return f"{tool_name} is only allowed as a deferred confirmation candidate"
    if tool_name == "lookup_side_effect_info" and not str(arguments.get("symptom_text") or "").strip():
        return "lookup_side_effect_info requires symptom_text"
    if tool_name == "AE_pro_ctcae" and not (
        str(arguments.get("symptom_text") or "").strip() or str(arguments.get("symptom_normalize") or "").strip()
    ):
        return "AE_pro_ctcae requires symptom_text or symptom_normalize"
    return None


def _validate_mark_dose_taken(arguments: dict[str, Any], *, source_event_type: str, payload: dict[str, Any]) -> str | None:
    if source_event_type != "multiturn_chat":
        return "mark_dose_taken is only allowed in multiturn_chat"
    dose_event_id = arguments.get("dose_event_id")
    if not isinstance(dose_event_id, int):
        return "mark_dose_taken requires integer dose_event_id"
    allowed_ids = _context_dose_event_ids(payload)
    if allowed_ids and dose_event_id not in allowed_ids:
        return "mark_dose_taken dose_event_id must match current chat context"
    return None


def _context_dose_event_ids(payload: dict[str, Any]) -> set[int]:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    candidates = []
    for key in ("today_dose_events", "dose_events"):
        value = context.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    ids: set[int] = set()
    for item in candidates:
        value = item.get("dose_event_id") or item.get("id")
        if isinstance(value, int):
            ids.add(value)
    return ids
