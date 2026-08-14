from __future__ import annotations

DELEGATE_TO_MEDICATION_AGENT = "delegate_to_medication_agent"
DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT = "delegate_to_nutrition_management_agent"
DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT = "delegate_to_nutrition_recommendation_agent"
REQUEST_RECORD_APPROVAL = "request_record_approval"

UPDATE_MEDICATION_DOSE_EVENT_STATUS = "update_medication_dose_event_status"
GET_MEDICATION_DOSE_STATUS = "get_medication_dose_status"
GET_MEDICATION_SIDE_EFFECT_ASSESSMENT = "get_medication_side_effect_assessment"
CREATE_MEDICATION_SIDE_EFFECT_RECORD = "create_medication_side_effect_record"
GET_SIDE_EFFECT_HISTORY = "get_side_effect_history"
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
GET_NOTIFICATION_POLICIES = "get_notification_policies"
CHANGE_NOTIFICATION_POLICY = "change_notification_policy"

SOURCE_DAILY_PATTERN = "daily_pattern"
SOURCE_MANUAL_DAILY_PATTERN = "manual_daily_pattern"
SOURCE_MISSED_DOSE = "missed_dose"
SOURCE_MULTITURN_CHAT = "multiturn_chat"
SOURCE_MEDICATION_AGENT = "medication_agent"
SOURCE_NUTRITION_MANAGEMENT_AGENT = "nutrition_management_agent"
SOURCE_NUTRITION_RECOMMENDATION_AGENT = "nutrition_recommendation_agent"
SOURCE_MCP = "mcp"

DELEGATION_TOOL_NAMES = {
    DELEGATE_TO_MEDICATION_AGENT,
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
}
SIDE_EFFECT_TOOLS = {GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE}
MEDICATION_SIDE_EFFECT_FEATURE_TOOLS = frozenset(
    {
        *SIDE_EFFECT_TOOLS,
        GET_SIDE_EFFECT_HISTORY,
        CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    }
)
MEDICATION_QUERY_TOOLS = {GET_MEDICATION_DOSE_STATUS, GET_SIDE_EFFECT_HISTORY}
POLICY_TOOLS = {PROPOSE_NOTIFICATION_POLICY, PROPOSE_SYSTEM_POLICY}
POLICY_QUERY_TOOLS = {GET_NOTIFICATION_POLICIES}
BACKEND_V13_RECORD_WRITE_TOOLS = frozenset(
    {
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        CREATE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
    }
)
BACKEND_V13_POLICY_WRITE_TOOLS = frozenset({CHANGE_NOTIFICATION_POLICY})
BACKEND_V13_SYNC_WRITE_TOOLS = BACKEND_V13_RECORD_WRITE_TOOLS | BACKEND_V13_POLICY_WRITE_TOOLS
RECORD_APPROVAL_ACTIONS = (
    BACKEND_V13_RECORD_WRITE_TOOLS
    | BACKEND_V13_POLICY_WRITE_TOOLS
    | frozenset({UPSERT_NUTRITION_PREFERENCE_FACT})
)
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
MEDICATION_CHAT_TOOLS = {
    REQUEST_RECORD_APPROVAL,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    *SIDE_EFFECT_TOOLS,
    *MEDICATION_QUERY_TOOLS,
}
MEDICATION_AGENT_CALLABLE_TOOLS = MEDICATION_CHAT_TOOLS - {
    GET_PRO_CTCAE_QUESTIONNAIRE,
}
NUTRITION_MANAGEMENT_TOOLS = {
    REQUEST_RECORD_APPROVAL,
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
ALL_TOOL_NAMES = (
    MEDICATION_CHAT_TOOLS
    | POLICY_TOOLS
    | POLICY_QUERY_TOOLS
    | NUTRITION_TOOLS
    | BACKEND_V13_POLICY_WRITE_TOOLS
)


def _metadata(
    domain: str,
    source_path: str,
    source_tool_name: str,
    mutability: str,
    risk_level: str,
    confirmation_policy: str | None = None,
) -> dict[str, str]:
    return {
        "domain": domain,
        "source_repo": "DrAnswer_Testbed",
        "source_path": source_path,
        "source_tool_name": source_tool_name,
        "mutability": mutability,
        "risk_level": risk_level,
        "confirmation_policy": confirmation_policy or ("user_required" if mutability in {"write", "delete"} else "app_server_confirmation" if mutability == "propose" else "none"),
    }


MODEL_VISIBLE_TOOL_METADATA: dict[str, dict[str, str]] = {
    DELEGATE_TO_MEDICATION_AGENT: _metadata("orchestration", "agent_app/orchestration/delegation.py", DELEGATE_TO_MEDICATION_AGENT, "none", "low"),
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT: _metadata("orchestration", "agent_app/orchestration/delegation.py", DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT, "none", "low"),
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT: _metadata("orchestration", "agent_app/orchestration/delegation.py", DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT, "none", "low"),
    REQUEST_RECORD_APPROVAL: _metadata(
        "record_approval",
        "agent_app/tools/mcp_server.py",
        REQUEST_RECORD_APPROVAL,
        "propose",
        "medium",
        confirmation_policy="user_required",
    ),
    UPDATE_MEDICATION_DOSE_EVENT_STATUS: _metadata("medication", "agent_app/tools/backend_write.py", UPDATE_MEDICATION_DOSE_EVENT_STATUS, "write", "medium"),
    GET_MEDICATION_DOSE_STATUS: _metadata("medication", "agent_app/tools/backend_query.py", GET_MEDICATION_DOSE_STATUS, "read", "low"),
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT: _metadata("medication_safety", "agent_app/tools/mcp_server.py", "snapshot_side_effect_assessment", "read", "medium"),
    CREATE_MEDICATION_SIDE_EFFECT_RECORD: _metadata("medication_safety", "agent_app/tools/backend_write.py", CREATE_MEDICATION_SIDE_EFFECT_RECORD, "write", "medium"),
    GET_SIDE_EFFECT_HISTORY: _metadata("medication_safety", "agent_app/tools/backend_query.py", GET_SIDE_EFFECT_HISTORY, "read", "medium"),
    GET_PRO_CTCAE_QUESTIONNAIRE: _metadata("medication_safety", "agent_app/ae_pro_ctcae.py", "match_pro_ctcae_symptom", "read", "medium"),
    SEARCH_NUTRITION_FOOD_CANDIDATES: _metadata("nutrition", "agent_app/tools/backend_query.py", SEARCH_NUTRITION_FOOD_CANDIDATES, "read", "low"),
    CREATE_NUTRITION_MEAL_RECORD: _metadata("nutrition", "agent_app/tools/backend_write.py", CREATE_NUTRITION_MEAL_RECORD, "write", "medium"),
    UPDATE_NUTRITION_MEAL_RECORD: _metadata("nutrition", "agent_app/tools/backend_write.py", UPDATE_NUTRITION_MEAL_RECORD, "write", "medium"),
    DELETE_NUTRITION_MEAL_RECORD: _metadata("nutrition", "agent_app/tools/backend_write.py", DELETE_NUTRITION_MEAL_RECORD, "delete", "medium"),
    UPDATE_NUTRITION_FOOD_RECORD: _metadata("nutrition", "agent_app/tools/backend_write.py", UPDATE_NUTRITION_FOOD_RECORD, "write", "medium"),
    DELETE_NUTRITION_FOOD_RECORD: _metadata("nutrition", "agent_app/tools/backend_write.py", DELETE_NUTRITION_FOOD_RECORD, "delete", "medium"),
    GET_NUTRITION_MEAL_RECORD_LIST: _metadata("nutrition", "agent_app/tools/backend_query.py", GET_NUTRITION_MEAL_RECORD_LIST, "read", "low"),
    GET_NUTRITION_DAILY_SUMMARY: _metadata("nutrition", "agent_app/tools/backend_query.py", GET_NUTRITION_DAILY_SUMMARY, "read", "low"),
    UPSERT_NUTRITION_PREFERENCE_FACT: _metadata("nutrition", "agent_app/tools/mcp_server.py", "AgentMcpToolServer._execute_tool_result", "write", "medium"),
    GET_NUTRITION_PREFERENCE_SUMMARY: _metadata("nutrition", "agent_app/tools/backend_query.py", GET_NUTRITION_PREFERENCE_SUMMARY, "read", "low"),
    GET_NUTRITION_RECOMMENDATION_CANDIDATES: _metadata("nutrition", "agent_app/tools/backend_query.py", GET_NUTRITION_RECOMMENDATION_CANDIDATES, "read", "low"),
    PROPOSE_NOTIFICATION_POLICY: _metadata("policy", "agent_app/tools/policy.py", "deferred_policy_tool_result", "propose", "high"),
    PROPOSE_SYSTEM_POLICY: _metadata("policy", "agent_app/tools/policy.py", "deferred_policy_tool_result", "propose", "high"),
    GET_NOTIFICATION_POLICIES: _metadata(
        "policy",
        "agent_app/tools/backend_query.py",
        GET_NOTIFICATION_POLICIES,
        "read",
        "low",
    ),
    CHANGE_NOTIFICATION_POLICY: _metadata(
        "policy",
        "agent_app/tools/backend_write.py",
        CHANGE_NOTIFICATION_POLICY,
        "write",
        "high",
        confirmation_policy="user_required",
    ),
}
