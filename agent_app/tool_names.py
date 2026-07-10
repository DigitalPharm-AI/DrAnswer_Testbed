from __future__ import annotations

import re
from typing import Any

DELEGATE_TO_MEDICATION_AGENT = "delegate_to_medication_agent"
DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT = "delegate_to_nutrition_management_agent"
DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT = "delegate_to_nutrition_recommendation_agent"

UPDATE_MEDICATION_DOSE_EVENT_STATUS = "update_medication_dose_event_status"
GET_MEDICATION_DOSE_EVENT_RECORD_LIST = "get_medication_dose_event_record_list"
GET_MEDICATION_SIDE_EFFECT_ASSESSMENT = "get_medication_side_effect_assessment"
GET_MEDICATION_SIDE_EFFECT_RECORD_LIST = "get_medication_side_effect_record_list"
GET_PRO_CTCAE_QUESTIONNAIRE = "get_pro_ctcae_questionnaire"

SEARCH_NUTRITION_FOOD_CANDIDATES = "search_nutrition_food_candidates"
CREATE_NUTRITION_MEAL_RECORD = "create_nutrition_meal_record"
UPDATE_NUTRITION_MEAL_RECORD = "update_nutrition_meal_record"
DELETE_NUTRITION_MEAL_RECORD = "delete_nutrition_meal_record"
UPDATE_NUTRITION_FOOD_RECORD = "update_nutrition_food_record"
DELETE_NUTRITION_FOOD_RECORD = "delete_nutrition_food_record"
GET_NUTRITION_MEAL_RECORD_LIST = "get_nutrition_meal_record_list"
GET_NUTRITION_DAILY_SUMMARY = "get_nutrition_daily_summary"
UPSERT_NUTRITION_PREFERENCE_FACT = "upsert_nutrition_preference_fact"
GET_NUTRITION_PREFERENCE_SUMMARY = "get_nutrition_preference_summary"
GET_NUTRITION_RECOMMENDATION_CANDIDATES = "get_nutrition_recommendation_candidates"

PROPOSE_NOTIFICATION_POLICY = "propose_notification_policy"
PROPOSE_SYSTEM_POLICY = "propose_system_policy"

SOURCE_DAILY_PATTERN = "daily_pattern"
SOURCE_MANUAL_DAILY_PATTERN = "manual_daily_pattern"
SOURCE_MISSED_DOSE = "missed_dose"
SOURCE_MULTITURN_CHAT = "multiturn_chat"
SOURCE_MEDICATION_AGENT = "medication_agent"
SOURCE_NUTRITION_MANAGEMENT_AGENT = "nutrition_management_agent"
SOURCE_NUTRITION_RECOMMENDATION_AGENT = "nutrition_recommendation_agent"
SOURCE_MCP = "mcp"

LEGACY_TO_CANONICAL_TOOL_NAMES = {
    "call_medication_agent": DELEGATE_TO_MEDICATION_AGENT,
    "call_nutrition_management_agent": DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    "call_nutrition_recommendation_agent": DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    "mark_dose_taken": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    "lookup_side_effect_info": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    "AE_pro_ctcae": GET_PRO_CTCAE_QUESTIONNAIRE,
    "search_food_nutrition": SEARCH_NUTRITION_FOOD_CANDIDATES,
    "record_meal": CREATE_NUTRITION_MEAL_RECORD,
    "update_nutrition_meal": UPDATE_NUTRITION_MEAL_RECORD,
    "delete_nutrition_meal": DELETE_NUTRITION_MEAL_RECORD,
    "update_nutrition_food": UPDATE_NUTRITION_FOOD_RECORD,
    "delete_nutrition_food": DELETE_NUTRITION_FOOD_RECORD,
    "list_meals": GET_NUTRITION_MEAL_RECORD_LIST,
    "get_daily_nutrition_summary": GET_NUTRITION_DAILY_SUMMARY,
    "record_nutrition_preference": UPSERT_NUTRITION_PREFERENCE_FACT,
    "get_nutrition_preferences": GET_NUTRITION_PREFERENCE_SUMMARY,
    "recommend_diet": GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    "apply_notification_policy": PROPOSE_NOTIFICATION_POLICY,
    "apply_system_policy": PROPOSE_SYSTEM_POLICY,
}

CANONICAL_TO_LEGACY_TOOL_NAMES = {value: key for key, value in LEGACY_TO_CANONICAL_TOOL_NAMES.items()}
LEGACY_TOOL_NAMES = frozenset(LEGACY_TO_CANONICAL_TOOL_NAMES)
_LEGACY_TOOL_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])("
    + "|".join(re.escape(name) for name in sorted(LEGACY_TO_CANONICAL_TOOL_NAMES, key=len, reverse=True))
    + r")(?![A-Za-z0-9_])"
)

DELEGATION_TOOL_NAMES = {
    DELEGATE_TO_MEDICATION_AGENT,
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
}
SIDE_EFFECT_TOOLS = {GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE}
MEDICATION_QUERY_TOOLS = {GET_MEDICATION_DOSE_EVENT_RECORD_LIST, GET_MEDICATION_SIDE_EFFECT_RECORD_LIST}
POLICY_TOOLS = {PROPOSE_NOTIFICATION_POLICY, PROPOSE_SYSTEM_POLICY}
NUTRITION_TOOLS = {
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    CREATE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_DAILY_SUMMARY,
    UPSERT_NUTRITION_PREFERENCE_FACT,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
}
MEDICATION_CHAT_TOOLS = {UPDATE_MEDICATION_DOSE_EVENT_STATUS, *SIDE_EFFECT_TOOLS, *MEDICATION_QUERY_TOOLS}
NUTRITION_MANAGEMENT_TOOLS = {
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    CREATE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    UPDATE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_DAILY_SUMMARY,
    UPSERT_NUTRITION_PREFERENCE_FACT,
    GET_NUTRITION_PREFERENCE_SUMMARY,
}
NUTRITION_RECOMMENDATION_TOOLS = {
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
}
ALL_TOOL_NAMES = MEDICATION_CHAT_TOOLS | POLICY_TOOLS | NUTRITION_TOOLS


def _metadata(domain: str, source_path: str, source_tool_name: str, mutability: str, risk_level: str) -> dict[str, str]:
    return {
        "domain": domain,
        "source_repo": "DrAnswer_Testbed",
        "source_path": source_path,
        "source_tool_name": source_tool_name,
        "mutability": mutability,
        "risk_level": risk_level,
    }


MODEL_VISIBLE_TOOL_METADATA: dict[str, dict[str, str]] = {
    DELEGATE_TO_MEDICATION_AGENT: _metadata("orchestration", "agent_app/agent_delegation.py", DELEGATE_TO_MEDICATION_AGENT, "none", "low"),
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT: _metadata(
        "orchestration", "agent_app/agent_delegation.py", DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT, "none", "low"
    ),
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT: _metadata(
        "orchestration", "agent_app/agent_delegation.py", DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT, "none", "low"
    ),
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: _metadata(
        "medication", "system_app/routes/agent_api.py", "agent_update_medication_dose_event_status", "write", "medium"
    ),
    GET_MEDICATION_DOSE_EVENT_RECORD_LIST: _metadata(
        "medication", "system_app/routes/agent_api.py", "agent_medication_dose_status", "read", "low"
    ),
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT: _metadata(
        "medication_safety", "agent_app/tool_mcp_server.py", "phr_side_effect_assessment", "read", "medium"
    ),
    GET_MEDICATION_SIDE_EFFECT_RECORD_LIST: _metadata(
        "medication_safety", "system_app/routes/agent_api.py", "agent_side_effect_history", "read", "medium"
    ),
    GET_PRO_CTCAE_QUESTIONNAIRE: _metadata(
        "medication_safety", "agent_app/ae_pro_ctcae.py", "match_pro_ctcae_symptom", "read", "medium"
    ),
    SEARCH_NUTRITION_FOOD_CANDIDATES: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_search_nutrition_food_candidates", "read", "low"
    ),
    CREATE_NUTRITION_MEAL_RECORD: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_create_nutrition_meal_record", "write", "medium"
    ),
    UPDATE_NUTRITION_MEAL_RECORD: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_update_nutrition_meal_record", "write", "medium"
    ),
    DELETE_NUTRITION_MEAL_RECORD: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_delete_nutrition_meal_record", "delete", "medium"
    ),
    UPDATE_NUTRITION_FOOD_RECORD: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_update_nutrition_food_record", "write", "medium"
    ),
    DELETE_NUTRITION_FOOD_RECORD: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_delete_nutrition_food_record", "delete", "medium"
    ),
    GET_NUTRITION_MEAL_RECORD_LIST: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_get_nutrition_meal_record_list", "read", "low"
    ),
    GET_NUTRITION_DAILY_SUMMARY: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_get_nutrition_daily_summary", "read", "low"
    ),
    UPSERT_NUTRITION_PREFERENCE_FACT: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_upsert_nutrition_preference_fact", "write", "medium"
    ),
    GET_NUTRITION_PREFERENCE_SUMMARY: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_get_nutrition_preference_summary", "read", "low"
    ),
    GET_NUTRITION_RECOMMENDATION_CANDIDATES: _metadata(
        "nutrition", "system_app/routes/agent_api.py", "agent_get_nutrition_recommendation_candidates", "read", "low"
    ),
    PROPOSE_NOTIFICATION_POLICY: _metadata(
        "policy", "agent_app/tool_policy.py", "deferred_policy_tool_result", "propose", "high"
    ),
    PROPOSE_SYSTEM_POLICY: _metadata(
        "policy", "agent_app/tool_policy.py", "deferred_policy_tool_result", "propose", "high"
    ),
}


def canonical_tool_name(name: str) -> str:
    return LEGACY_TO_CANONICAL_TOOL_NAMES.get(str(name or ""), str(name or ""))


def legacy_tool_name(name: str) -> str:
    return CANONICAL_TO_LEGACY_TOOL_NAMES.get(str(name or ""), str(name or ""))


def replace_legacy_tool_names(value: Any) -> Any:
    if isinstance(value, str):
        return _LEGACY_TOOL_NAME_PATTERN.sub(lambda match: LEGACY_TO_CANONICAL_TOOL_NAMES[match.group(1)], value)
    if isinstance(value, list):
        return [replace_legacy_tool_names(item) for item in value]
    if isinstance(value, dict):
        return {key: replace_legacy_tool_names(item) for key, item in value.items()}
    return value
