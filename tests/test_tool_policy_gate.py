from agent_app.tools.policy_gate import (
    ToolCallContext,
    ToolCallOrigin,
    ToolPolicyGate,
    tool_call_fingerprint,
)
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    SOURCE_MEDICATION_AGENT,
    SOURCE_NUTRITION_MANAGEMENT_AGENT,
)


def test_safety_rule_only_allows_pro_ctcae_questionnaire() -> None:
    gate = ToolPolicyGate()
    allowed = gate.authorize(
        {
            "name": GET_PRO_CTCAE_QUESTIONNAIRE,
            "arguments": {"symptom_text": "메스꺼움"},
        },
        trace_id="trace-safety-allowed",
        source_event_type=SOURCE_MEDICATION_AGENT,
        payload={"context": {}},
        context=ToolCallContext(origin=ToolCallOrigin.SAFETY_RULE),
    )
    denied = gate.authorize(
        {
            "name": GET_SIDE_EFFECT_HISTORY,
            "arguments": {"limit": 5},
        },
        trace_id="trace-safety-denied",
        source_event_type=SOURCE_MEDICATION_AGENT,
        payload={"context": {}},
        context=ToolCallContext(origin=ToolCallOrigin.SAFETY_RULE),
    )

    assert allowed is None
    assert denied is not None
    assert denied.status == "error"
    assert denied.response["reason"].startswith(
        "safety_rule may only invoke"
    )


def test_approved_write_must_match_confirmed_action() -> None:
    gate = ToolPolicyGate()
    tool_call = {
        "name": CREATE_NUTRITION_MEAL_RECORD,
        "arguments": {
            "meal_type": "lunch",
            "foods": [{"food_name": "삶은 계란"}],
        },
    }
    denied = gate.authorize(
        tool_call,
        trace_id="trace-approved-mismatch",
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
        payload={
            "context": {
                "approved_user_action": {
                    "status": "confirmed",
                    "action_name": CREATE_NUTRITION_MEAL_RECORD,
                    "arguments": {
                        "meal_type": "lunch",
                        "foods": [{"food_name": "다른 음식"}],
                    },
                }
            }
        },
        context=ToolCallContext(
            origin=ToolCallOrigin.APPROVED_WRITE
        ),
    )

    assert denied is not None
    assert denied.status == "error"
    assert denied.response["reason"] == (
        "approved_write Tool arguments do not match confirmation"
    )


def test_approved_write_accepts_only_server_injected_short_key() -> None:
    gate = ToolPolicyGate()
    call = {
        "name": CREATE_NUTRITION_MEAL_RECORD,
        "arguments": {"approval_key": "apv_abcdefghijklmnopqrstuvwx"},
    }
    allowed = gate.authorize(
        call,
        trace_id="trace-approved-key",
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
        payload={
            "context": {
                "approved_user_action": {
                    "status": "confirmed",
                    "action_name": CREATE_NUTRITION_MEAL_RECORD,
                    "arguments": dict(call["arguments"]),
                }
            }
        },
        context=ToolCallContext(origin=ToolCallOrigin.APPROVED_WRITE),
    )
    assert allowed is None


def test_exact_prior_tool_call_is_skipped_at_gateway() -> None:
    gate = ToolPolicyGate()
    tool_call = {
        "name": GET_SIDE_EFFECT_HISTORY,
        "arguments": {"limit": 5},
    }
    duplicate = gate.authorize(
        tool_call,
        trace_id="trace-duplicate",
        source_event_type=SOURCE_MEDICATION_AGENT,
        payload={"context": {}},
        context=ToolCallContext(
            origin=ToolCallOrigin.MODEL,
            prior_call_fingerprints=frozenset(
                {tool_call_fingerprint(tool_call)}
            ),
        ),
    )

    assert duplicate is not None
    assert duplicate.status == "skipped"
    assert duplicate.response == {"reason": "duplicate_tool_call"}
