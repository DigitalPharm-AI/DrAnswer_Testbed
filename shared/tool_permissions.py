from __future__ import annotations

from datetime import date
from typing import Any

from shared.schemas import ToolCallResult
from shared.tool_names import (
    BACKEND_V13_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    MEDICATION_CHAT_TOOLS,
    NUTRITION_MANAGEMENT_TOOLS,
    NUTRITION_RECOMMENDATION_TOOLS,
    POLICY_TOOLS,
    PROPOSE_NOTIFICATION_POLICY,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    SIDE_EFFECT_TOOLS,
    SOURCE_DAILY_PATTERN,
    SOURCE_MANUAL_DAILY_PATTERN,
    SOURCE_MCP,
    SOURCE_MEDICATION_AGENT,
    SOURCE_MISSED_DOSE,
    SOURCE_MULTITURN_CHAT,
    SOURCE_NUTRITION_MANAGEMENT_AGENT,
    SOURCE_NUTRITION_RECOMMENDATION_AGENT,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)

HIGH_RISK_HUMAN_HANDOFF_TOOLS = POLICY_TOOLS

TOOL_ALLOWLIST: dict[str, set[str]] = {
    SOURCE_DAILY_PATTERN: {PROPOSE_NOTIFICATION_POLICY},
    SOURCE_MANUAL_DAILY_PATTERN: {PROPOSE_NOTIFICATION_POLICY},
    SOURCE_MISSED_DOSE: SIDE_EFFECT_TOOLS,
    SOURCE_MULTITURN_CHAT: {
        REQUEST_RECORD_APPROVAL,
        CHANGE_NOTIFICATION_POLICY,
        GET_NOTIFICATION_POLICIES,
        *POLICY_TOOLS,
    },
    SOURCE_MEDICATION_AGENT: MEDICATION_CHAT_TOOLS,
    SOURCE_NUTRITION_MANAGEMENT_AGENT: NUTRITION_MANAGEMENT_TOOLS,
    SOURCE_NUTRITION_RECOMMENDATION_AGENT: NUTRITION_RECOMMENDATION_TOOLS,
    # The generic /agent/mcp source is intentionally read-only. Specialist
    # agents use their explicit source types, and confirmed mutations are
    # executed through those trusted server-owned paths.
    SOURCE_MCP: {
        GET_PRO_CTCAE_QUESTIONNAIRE,
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        GET_NUTRITION_MEAL_RECORD_LIST,
        GET_NUTRITION_DAILY_SUMMARY,
        GET_NUTRITION_PREFERENCE_SUMMARY,
        GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    },
}


def allowed_tool_names_for_source(source_event_type: str) -> set[str]:
    return set(TOOL_ALLOWLIST.get(source_event_type, TOOL_ALLOWLIST.get(SOURCE_MCP, set())))


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
    if "patient_id" in arguments:
        return f"{tool_name} patient_id is managed inside the AI Server Tool"
    if tool_name == REQUEST_RECORD_APPROVAL:
        action_name = str(arguments.get("action_name") or "").strip()
        record_arguments = arguments.get("record_arguments")
        if action_name not in RECORD_APPROVAL_ACTIONS:
            return f"{REQUEST_RECORD_APPROVAL} requires a supported record action_name"
        if action_name not in allowed_tools:
            return f"{REQUEST_RECORD_APPROVAL} action {action_name} is not allowed for {source_event_type}"
        if not isinstance(record_arguments, dict):
            return f"{REQUEST_RECORD_APPROVAL} requires record_arguments"
        nested_error = validate_tool_permission(
            {"name": action_name, "arguments": record_arguments},
            source_event_type=source_event_type,
            payload=payload,
        )
        return (
            f"{REQUEST_RECORD_APPROVAL} record_arguments invalid: {nested_error}"
            if nested_error
            else None
        )
    if tool_name in BACKEND_V13_SYNC_WRITE_TOOLS:
        forbidden = {
            "expected_version",
            "request_id",
            "source_chat_request_id",
            "confirmation_message_id",
            "requested_at",
            "reason",
        }.intersection(arguments)
        if forbidden:
            return f"{tool_name} technical arguments are managed inside the AI Server Tool: {', '.join(sorted(forbidden))}"
    if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return _validate_dose_event_status_update(
            arguments,
            source_event_type=source_event_type,
            payload=payload,
        )
    if tool_name in POLICY_TOOLS and source_event_type not in {SOURCE_DAILY_PATTERN, SOURCE_MANUAL_DAILY_PATTERN, SOURCE_MULTITURN_CHAT}:
        return f"{tool_name} is only allowed as a deferred confirmation candidate"
    if tool_name == CHANGE_NOTIFICATION_POLICY:
        if source_event_type != SOURCE_MULTITURN_CHAT:
            return f"{CHANGE_NOTIFICATION_POLICY} is only allowed in multiturn_chat"
        if not str(arguments.get("policy_id") or "").strip():
            return f"{CHANGE_NOTIFICATION_POLICY} requires policy_id"
        decision = arguments.get("decision")
        if decision not in {"apply", "keep"}:
            return f"{CHANGE_NOTIFICATION_POLICY} requires apply or keep decision"
        changes = arguments.get("changes")
        if decision == "apply" and (not isinstance(changes, dict) or not changes):
            return f"{CHANGE_NOTIFICATION_POLICY} requires changes for apply"
        if decision == "keep" and changes is not None:
            return f"{CHANGE_NOTIFICATION_POLICY} does not accept changes for keep"
    if tool_name == GET_NOTIFICATION_POLICIES:
        if "policy_id" in arguments and not str(arguments.get("policy_id") or "").strip():
            return f"{GET_NOTIFICATION_POLICIES} requires non-empty policy_id when provided"
        if "slot_label" in arguments and not str(arguments.get("slot_label") or "").strip():
            return f"{GET_NOTIFICATION_POLICIES} requires non-empty slot_label when provided"
        if "active_only" in arguments and not isinstance(arguments.get("active_only"), bool):
            return f"{GET_NOTIFICATION_POLICIES} requires active_only boolean"
    if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT and not str(arguments.get("symptom_text") or "").strip():
        return f"{GET_MEDICATION_SIDE_EFFECT_ASSESSMENT} requires symptom_text"
    if tool_name == CREATE_MEDICATION_SIDE_EFFECT_RECORD:
        if not str(arguments.get("symptom_text") or "").strip():
            return f"{CREATE_MEDICATION_SIDE_EFFECT_RECORD} requires symptom_text"
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
    if tool_name == GET_PRO_CTCAE_QUESTIONNAIRE and not str(arguments.get("symptom_text") or "").strip():
        return f"{GET_PRO_CTCAE_QUESTIONNAIRE} requires symptom_text"
    if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES:
        food_queries = arguments.get("food_queries")
        if (
            not isinstance(food_queries, list)
            or not 1 <= len(food_queries) <= 8
            or any(
                not isinstance(query, str)
                or not query.strip()
                or len(query.strip()) > 100
                for query in food_queries
            )
            or len(
                {
                    query.strip()
                    for query in food_queries
                    if isinstance(query, str)
                }
            )
            != len(food_queries)
        ):
            return (
                f"{SEARCH_NUTRITION_FOOD_CANDIDATES} requires "
                "1 to 8 unique food_queries"
            )
        limit_per_query = arguments.get("limit_per_query")
        if (
            limit_per_query is not None
            and not _valid_positive_int(
                limit_per_query,
                maximum=20,
            )
        ):
            return (
                f"{SEARCH_NUTRITION_FOOD_CANDIDATES} requires "
                "limit_per_query between 1 and 20"
            )
    if tool_name == SEARCH_NUTRITION_FOOD_CANDIDATES and arguments.get("meal_type") not in {None, "", "breakfast", "lunch", "dinner", "snack"}:
        return f"{SEARCH_NUTRITION_FOOD_CANDIDATES} requires supported meal_type"
    if tool_name == CREATE_NUTRITION_MEAL_RECORD:
        if arguments.get("meal_type") not in {"breakfast", "lunch", "dinner", "snack"}:
            return f"{CREATE_NUTRITION_MEAL_RECORD} requires meal_type"
        if not isinstance(arguments.get("foods"), list) or not arguments.get("foods"):
            return f"{CREATE_NUTRITION_MEAL_RECORD} requires foods"
    if tool_name == UPDATE_NUTRITION_MEAL_RECORD:
        if not _valid_external_id(arguments.get("meal_id")):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires non-empty meal_id"
        if "meal_type" in arguments and arguments.get("meal_type") not in {None, "breakfast", "lunch", "dinner", "snack"}:
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires supported meal_type"
        if "foods" in arguments and (not isinstance(arguments.get("foods"), list) or not arguments.get("foods")):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires non-empty foods when foods is provided"
        if not any(key in arguments for key in ("meal_type", "meal_date", "meal_time", "scenario_key", "description", "foods")):
            return f"{UPDATE_NUTRITION_MEAL_RECORD} requires at least one update field"
    if tool_name == DELETE_NUTRITION_MEAL_RECORD:
        if not _valid_external_id(arguments.get("meal_id")):
            return f"{DELETE_NUTRITION_MEAL_RECORD} requires non-empty meal_id"
    if tool_name == UPDATE_NUTRITION_FOOD_RECORD:
        if not _valid_external_id(arguments.get("meal_id")):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires non-empty meal_id"
        if not _valid_external_id(arguments.get("food_id")):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires non-empty food_id"
        if not any(key in arguments for key in ("food_ref_id", "food_name", "portion", "nutrients")):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires at least one update field"
        if "food_name" in arguments and not str(arguments.get("food_name") or "").strip():
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires non-empty food_name when food_name is provided"
        if "nutrients" in arguments and not isinstance(arguments.get("nutrients"), dict):
            return f"{UPDATE_NUTRITION_FOOD_RECORD} requires nutrients object when nutrients is provided"
    if tool_name == DELETE_NUTRITION_FOOD_RECORD:
        if not _valid_external_id(arguments.get("meal_id")):
            return f"{DELETE_NUTRITION_FOOD_RECORD} requires non-empty meal_id"
        if not _valid_external_id(arguments.get("food_id")):
            return f"{DELETE_NUTRITION_FOOD_RECORD} requires non-empty food_id"
    if tool_name == GET_NUTRITION_RECOMMENDATION_CANDIDATES:
        if not isinstance(arguments.get("constraints"), dict) or not arguments.get("constraints"):
            return f"{GET_NUTRITION_RECOMMENDATION_CANDIDATES} requires constraints"
    if tool_name == UPSERT_NUTRITION_PREFERENCE_FACT:
        if arguments.get("predicate") not in {
            "likes",
            "dislikes",
            "prefers",
            "avoids_by_preference",
            "cannot_consume",
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


def _valid_external_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _validate_dose_event_status_update(arguments: dict[str, Any], *, source_event_type: str, payload: dict[str, Any]) -> str | None:
    if source_event_type != SOURCE_MEDICATION_AGENT:
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is only allowed in medication_agent"
    dose_event_id = arguments.get("dose_event_id")
    if not _valid_external_id(dose_event_id):
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} requires non-empty dose_event_id"
    allowed_ids = _context_dose_event_ids(payload)
    if allowed_ids and str(dose_event_id) not in allowed_ids:
        return f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} dose_event_id must match current chat context"
    return None


def _context_dose_event_ids(payload: dict[str, Any]) -> set[str]:
    context = payload.get("context") if isinstance(payload.get("context"), dict) else {}
    candidates = []
    for key in ("today_dose_events", "dose_events"):
        value = context.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    ids: set[str] = set()
    for item in candidates:
        value = item.get("dose_event_id") or item.get("id")
        if _valid_external_id(value):
            ids.add(str(value))
    return ids
