from agent_app.tools.policy import normalize_policy_tool_calls


def test_exact_duplicate_tool_calls_are_executed_once() -> None:
    calls = [
        {
            "id": "tool-call-1",
            "name": "get_medication_side_effect_assessment",
            "arguments": {
                "symptom_text": "메스꺼움",
                "medication_name": "메트포르민 500mg",
            },
        },
        {
            "id": "tool-call-2",
            "name": "get_medication_side_effect_assessment",
            "arguments": {
                "medication_name": "메트포르민 500mg",
                "symptom_text": "메스꺼움",
            },
        },
    ]

    normalized = normalize_policy_tool_calls(
        calls,
        source_event_type="medication_agent",
    )

    assert normalized == [calls[0]]


def test_same_tool_with_different_arguments_is_preserved() -> None:
    calls = [
        {
            "id": "tool-call-1",
            "name": "get_medication_side_effect_assessment",
            "arguments": {"symptom_text": "메스꺼움"},
        },
        {
            "id": "tool-call-2",
            "name": "get_medication_side_effect_assessment",
            "arguments": {"symptom_text": "구토"},
        },
    ]

    normalized = normalize_policy_tool_calls(
        calls,
        source_event_type="medication_agent",
    )

    assert normalized == calls
