from __future__ import annotations

from datetime import date
from typing import Any

from agent_app.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    MEDICATION_CHAT_TOOLS,
    NUTRITION_MANAGEMENT_TOOLS,
    NUTRITION_RECOMMENDATION_TOOLS,
    NUTRITION_TOOLS,
    POLICY_TOOLS,
    PROPOSE_NOTIFICATION_POLICY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    SIDE_EFFECT_TOOLS,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.schemas import ToolCallResult

HIGH_RISK_HUMAN_HANDOFF_TOOLS = POLICY_TOOLS

TOOL_ALLOWLIST: dict[str, set[str]] = {
    "daily_pattern": {PROPOSE_NOTIFICATION_POLICY},
    "manual_daily_pattern": {PROPOSE_NOTIFICATION_POLICY},
    "missed_dose": SIDE_EFFECT_TOOLS,
    "multiturn_chat": {*MEDICATION_CHAT_TOOLS, *POLICY_TOOLS, *NUTRITION_TOOLS},
    "mcp": {GET_PRO_CTCAE_QUESTIONNAIRE, *NUTRITION_TOOLS},
}


def allowed_tool_names_for_source(source_event_type: str) -> set[str]:
    return set(TOOL_ALLOWLIST.get(source_event_type, TOOL_ALLOWLIST.get("mcp", set())))


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
    allowed_tools = allowed_tool_names_for_source(source_event_type)
    if tool_name not in allowed_tools:
        return f"{tool_name or 'unknown'} is not allowed for {source_event_type}"
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return _validate_mark_dose_taken(arguments, source_event_type=source_event_type, payload=payload)
    if tool_name in POLICY_TOOLS and source_event_type not in {"daily_pattern", "manual_daily_pattern", "multiturn_chat"}:
        return f"{tool_name} is only allowed as a deferred confirmation candidate"
    if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT and not str(arguments.get("symptom_text") or "").strip():
        return f"{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT} requires symptom_text"
    if tool_name == GET_SIDE_EFFECT_HISTORY and "limit" in arguments and not _valid_positive_int(arguments.get("limit"), maximum=100):
        return f"{GET_SIDE_EFFECT_HISTORY} requires limit between 1 and 100"
    if tool_name == GET_SIDE_EFFECT_HISTORY:
        date_error = _validate_date_range_arguments(arguments, tool_name=GET_SIDE_EFFECT_HISTORY, max_days=366)
        if date_error:
            return date_error
        if arguments.get("severity") not in {None, "", "none", "low", "moderate", "high"}:
            return f"{GET_SIDE_EFFECT_HISTORY} requires supported severity"
    if tool_name == GET_MEDICATION_DOSE_STATUS:
        date_error = _validate_date_range_arguments(arguments, tool_name=GET_MEDICATION_DOSE_STATUS, max_days=31)
        if date_error:
            return date_error
        if arguments.get("status") not in {None, "", "scheduled", "taken", "missed"}:
            return f"{GET_MEDICATION_DOSE_STATUS} requires supported status"
    if tool_name == GET_PRO_CTCAE_QUESTIONNAIRE and not (
        str(arguments.get("symptom_text") or "").strip() or str(arguments.get("symptom_normalize") or "").strip()
    ):
        return f"{GET_PRO_CTCAE_QUESTIONNAIRE} requires symptom_text or symptom_normalize"
    if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES and not str(arguments.get("query") or "").strip():
        return f"{SEARCH_NUTRITION_FOOD_CANDIDATES} requires query"
    if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES and arguments.get("meal_type") not in {None, "", "breakfast", "lunch", "dinner", "snack"}:
        return f"{SEARCH_NUTRITION_FOOD_CANDIDATES} requires supported meal_type"
    if tool_name == CREATE_NUTRITION_MEAL_RECORD:
        if arguments.get("meal_type") not in {"breakfast", "lunch", "dinner", "snack"}:
            return f"{CREATE_NUTRITION_MEAL_RECORD} requires meal_type"
        if not isinstance(arguments.get("foods"), list) or not arguments.get("foods"):
            return f"{CREATE_NUTRITION_MEAL_RECORD} requires foods"
    if tool_name == UPDATE_NUTRITION_MEAL_RECORD:
        if not isinstance(arguments.get("meal_id"), int):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires integer meal_id"
        if "meal_type" in arguments and arguments.get("meal_type") not in {None, "breakfast", "lunch", "dinner", "snack"}:
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires supported meal_type"
        if "foods" in arguments and (not isinstance(arguments.get("foods"), list) or not arguments.get("foods")):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires non-empty foods when foods is provided"
        if not any(key in arguments for key in ("meal_type", "meal_date", "meal_time", "scenario_key", "description", "foods")):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires at least one update field"
    if tool_name == DELETE_NUTRITION_MEAL_RECORD:
        if not isinstance(arguments.get("meal_id"), int):
            return f"{DELETE_NUTRITION_MEAL_RECORD} requires integer meal_id"
    if tool_name == UPDATE_NUTRITION_FOOD_RECORD:
        if not isinstance(arguments.get("meal_id"), int):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires integer meal_id"
        if not isinstance(arguments.get("food_id"), int):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires integer food_id"
        if not any(key in arguments for key in ("food_ref_id", "food_name", "portion", "nutrients")):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires at least one update field"
        if "food_name" in arguments and not str(arguments.get("food_name") or "").strip():
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires non-empty food_name when food_name is provided"
        if "nutrients" in arguments and not isinstance(arguments.get("nutrients"), dict):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires nutrients object when nutrients is provided"
    if tool_name == DELETE_NUTRITION_FOOD_RECORD:
        if not isinstance(arguments.get("meal_id"), int):
            return f"{DELETE_NUTRITION_FOOD_RECORD} requires integer meal_id"
        if not isinstance(arguments.get("food_id"), int):
            return f"{DELETE_NUTRITION_FOOD_RECORD} requires integer food_id"
    if tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
        if not isinstance(arguments.get("constraints"), dict) or not arguments.get("constraints"):
            return f"{GET_NUTRITION_RECOMMENDATION_CANDIDATES} requires constraints"
    if tool_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        if arguments.get("predicate") not in {
            "likes",
            "dislikes",
            "prefers",
            "avoids_by_preference",
            "allergic_to",
            "medically_avoids",
            "religious_avoids",
        }:
            return f"{UPSERT_NUTRITION_PREFERENCE_FACT} requires supported predicate"
        if not str(arguments.get("object_label") or "").strip():
            return f"{UPSERT_NUTRITION_PREFERENCE_FACT} requires object_label"
    return None


def _valid_positive_int(value: Any, *, maximum: int) -> bool:
    return isinstance(value, int) and 1 <= value <= maximum


def _validate_date_range_arguments(arguments: dict[str, Any], *, tool_name: str, max_days: int) -> str | None:
    target_date = str(arguments.get("target_date") or "").strip()
    start_date = str(arguments.get("start_date") or "").strip()
    end_date = str(arguments.get("end_date") or "").strip()
    if "target_date" in arguments and not target_date:
        return f"{tool_name} requires non-empty target_date when target_date is provided"
    if target_date and (start_date or end_date):
        return f"{tool_name} requires either target_date or start_date/end_date, not both"
    if bool(start_date) != bool(end_date):
        return f"{tool_name} requires start_date and end_date together"
    if target_date:
        return None if _parse_iso_date(target_date) is not None else f"{tool_name} requires YYYY-MM-DD target_date"
    if start_date and end_date:
        parsed_start = _parse_iso_date(start_date)
        parsed_end = _parse_iso_date(end_date)
        if parsed_start is None or parsed_end is None:
            return f"{tool_name} requires YYYY-MM-DD start_date and end_date"
        if parsed_end < parsed_start:
            return f"{tool_name} requires end_date on or after start_date"
        if (parsed_end - parsed_start).days + 1 > max_days:
            return f"{tool_name} date range is too large"
    return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def requires_human_handoff(tool_name: str) -> bool:
    return str(tool_name or "") in HIGH_RISK_HUMAN_HANDOFF_TOOLS


def _validate_mark_dose_taken(arguments: dict[str, Any], *, source_event_type: str, payload: dict[str, Any]) -> str | None:
    if source_event_type != "multiturn_chat":
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is only allowed in multiturn_chat"
    dose_event_id = arguments.get("dose_event_id")
    if not isinstance(dose_event_id, int):
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} requires integer dose_event_id"
    allowed_ids = _context_dose_event_ids(payload)
    if allowed_ids and dose_event_id not in allowed_ids:
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} dose_event_id must match current chat context"
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
