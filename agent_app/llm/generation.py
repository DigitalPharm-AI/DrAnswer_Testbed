from __future__ import annotations

from pydantic import ValidationError

from agent_app.errors import AgentExecutionError
from agent_app.llm.messages import (
    LlmOutputParseError,
    LlmToolArgumentsParseError,
)

PROMPT_VERSION_ID = "agent_app_v2_tool_runtime"


def agent_error(trace_id: str, agent_name: str, decision_type: str, exc: Exception) -> AgentExecutionError:
    if isinstance(exc, AgentExecutionError):
        return exc
    if isinstance(exc, LlmOutputParseError):
        return AgentExecutionError(
            str(exc),
            error_type="llm_output_parse_failed",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )
    if isinstance(exc, LlmToolArgumentsParseError):
        return AgentExecutionError(
            str(exc),
            error_type="llm_tool_arguments_parse_failed",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )
    if isinstance(exc, ValidationError):
        return AgentExecutionError(
            str(exc),
            error_type="payload_validation_failed",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )
    return AgentExecutionError(
        str(exc),
        error_type="provider_request_failed",
        trace_id=trace_id,
        agent_name=agent_name,
        decision_type=decision_type,
    )
