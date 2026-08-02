from __future__ import annotations

from datetime import date
from typing import Any

from shared.schemas import ToolCallResult
from shared.tool_names import (
    BACKEND_V13_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    GET_MEDICATION_DOSE_STATUS,
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
    PROPOSE_SYSTEM_POLICY,
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
        PROPOSE_SYSTEM_POLICY,
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
    raw_arguments = tool_call.get("arguments", {})
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    if "patient_id" in arguments:
        return f"{tool_name} patient_id is managed inside the AI Server Tool"
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

    if tool_name == REQUEST_RECORD_APPROVAL:
        action_name = str(arguments.get("action_name") or "").strip()
        record_arguments = arguments.get("record_arguments")
        if action_name not in allowed_tools:
            return f"{REQUEST_RECORD_APPROVAL} action {action_name} is not allowed for {source_event_type}"
        # A medication name is only a resolution hint at the approval
        # boundary.  The MCP server replaces it with a trusted dose_event_id
        # before preparing approval.  Direct writes still require the ID.
        if (
            action_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS
            and isinstance(record_arguments, dict)
            and not _valid_external_id(
                record_arguments.get("dose_event_id")
            )
        ):
            medication_name = record_arguments.get(
                "medication_name"
            )
            unsupported = set(record_arguments).difference(
                {"medication_name"}
            )
            if unsupported:
                return (
                    f"{REQUEST_RECORD_APPROVAL} record_arguments "
                    "contain unsupported medication resolution fields: "
                    + ", ".join(sorted(unsupported))
                )
            if medication_name is not None and not _valid_external_id(
                medication_name
            ):
                return (
                    f"{REQUEST_RECORD_APPROVAL} medication_name must "
                    "be non-empty when provided"
                )
            return None
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
        decision = arguments.get("decision")
        changes = arguments.get("changes")
        if decision == "apply" and (not isinstance(changes, dict) or not changes):
            return f"{CHANGE_NOTIFICATION_POLICY} requires changes for apply"
        if decision == "keep" and changes is not None:
            return f"{CHANGE_NOTIFICATION_POLICY} does not accept changes for keep"
    if tool_name == GET_SIDE_EFFECT_HISTORY:
        date_error = _validate_date_range_arguments(arguments, tool_name=GET_SIDE_EFFECT_HISTORY, max_days=366)
        if date_error:
            return date_error
    if tool_name == GET_MEDICATION_DOSE_STATUS:
        date_error = _validate_date_range_arguments(arguments, tool_name=GET_MEDICATION_DOSE_STATUS, max_days=31)
        if date_error:
            return date_error
    return None


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
    trusted = context.get("trusted_patient_context")
    if isinstance(trusted, dict):
        today_medication = trusted.get("today_medication")
        if isinstance(today_medication, dict):
            dose_events = today_medication.get("dose_events")
            if isinstance(dose_events, list):
                candidates.extend(
                    item
                    for item in dose_events
                    if isinstance(item, dict)
                )
    ids: set[str] = set()
    for item in candidates:
        value = item.get("dose_event_id") or item.get("id")
        if _valid_external_id(value):
            ids.add(str(value))
    return ids
