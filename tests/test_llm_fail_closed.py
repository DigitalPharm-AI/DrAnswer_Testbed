from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from agent_app.errors import (
    AgentExecutionError,
    public_processing_error,
)
from agent_app.llm.generation import agent_error
from agent_app.llm.messages import (
    LlmOutputParseError,
    LlmToolArgumentsParseError,
    ai_message_from_tool_calls,
    model_output_from_ai_message,
    public_text_from_ai_message,
    tool_calls_from_ai_message,
)


def test_structured_model_output_rejects_plain_text_instead_of_falling_back():
    message = AIMessage(content="일반 텍스트 응답")

    with pytest.raises(
        LlmOutputParseError,
        match="llm_output_invalid_json_object",
    ):
        model_output_from_ai_message(message)


def test_structured_model_output_accepts_json_object():
    message = AIMessage(content='{"message":"정상 응답"}')

    assert model_output_from_ai_message(message) == {
        "message": "정상 응답",
    }


def test_general_agent_loop_reads_only_public_text_blocks():
    message = AIMessage(
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": {"text": "PRIVATE_REASONING"},
            },
            {"type": "text", "text": "사용자에게 보이는 응답"},
        ]
    )

    assert public_text_from_ai_message(message) == "사용자에게 보이는 응답"


def test_invalid_native_tool_call_is_rejected_instead_of_becoming_empty_args():
    message = AIMessage(
        content="",
        invalid_tool_calls=[
            {
                "name": "lookup",
                "args": "{invalid",
                "id": "call-1",
                "error": "invalid JSON",
            }
        ],
    )

    with pytest.raises(
        LlmToolArgumentsParseError,
        match="llm_tool_arguments_invalid",
    ):
        tool_calls_from_ai_message(message)


def test_internal_tool_call_requires_object_arguments():
    with pytest.raises(
        LlmToolArgumentsParseError,
        match="llm_tool_arguments_missing_or_invalid",
    ):
        ai_message_from_tool_calls(
            [{"name": "lookup", "arguments": "invalid"}],
        )


@pytest.mark.parametrize(
    ("exc", "error_type"),
    [
        (
            LlmOutputParseError("invalid output"),
            "llm_output_parse_failed",
        ),
        (
            LlmToolArgumentsParseError("invalid arguments"),
            "llm_tool_arguments_parse_failed",
        ),
    ],
)
def test_parse_failures_keep_distinct_internal_error_types(
    exc: Exception,
    error_type: str,
):
    converted = agent_error(
        "trace-internal",
        "test-agent",
        "test-decision",
        exc,
    )

    assert converted.error_type == error_type


@pytest.mark.parametrize(
    ("error_type", "code", "message", "retryable"),
    [
        (
            "llm_output_parse_failed",
            "LLM_OUTPUT_PARSE_FAILED",
            "The LLM response could not be parsed as the required JSON object.",
            True,
        ),
        (
            "llm_tool_arguments_parse_failed",
            "LLM_TOOL_ARGUMENTS_INVALID",
            "The LLM returned invalid Tool arguments.",
            True,
        ),
        (
            "llm_output_validation_failed",
            "LLM_OUTPUT_VALIDATION_FAILED",
            "The LLM response did not satisfy the required schema.",
            True,
        ),
        (
            "llm_final_answer_missing",
            "LLM_FINAL_ANSWER_MISSING",
            "The LLM did not return a final answer.",
            True,
        ),
        (
            "provider_request_failed",
            "LLM_PROVIDER_REQUEST_FAILED",
            "The LLM provider request failed.",
            True,
        ),
        (
            "missed_dose_policy_context_invalid",
            "MISSED_DOSE_POLICY_CONTEXT_INVALID",
            "The missed-dose policy context is invalid or incomplete.",
            False,
        ),
    ],
)
def test_known_agent_failures_have_safe_specific_public_errors(
    error_type: str,
    code: str,
    message: str,
    retryable: bool,
):
    internal = AgentExecutionError(
        "provider secret diagnostic",
        error_type=error_type,
        trace_id="trace-internal",
        agent_name="test-agent",
        decision_type="test-decision",
    )

    public = public_processing_error(internal)

    assert (public.code, public.message, public.retryable) == (
        code,
        message,
        retryable,
    )
    assert "secret" not in public.message
    assert "trace-internal" not in public.message
