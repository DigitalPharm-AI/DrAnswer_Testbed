from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError
from agent_app.output_validation import safe_fallback_output, validate_llm_output
from agent_app.providers import BaseLLMProvider

PROMPT_VERSION_ID = "agent_app_v2_tool_runtime"


def agent_error(trace_id: str, agent_name: str, decision_type: str, exc: Exception) -> AgentExecutionError:
    if isinstance(exc, AgentExecutionError):
        return exc
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


async def generate_llm_output(
    provider: BaseLLMProvider,
    trace_id: str,
    agent_name: str,
    decision_type: str,
    system_prompt: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = await provider.generate_json(system_prompt, payload)
    except Exception as exc:
        raise agent_error(trace_id, agent_name, decision_type, exc) from exc
    if not isinstance(result, dict):
        result = {}
    try:
        return validate_llm_output(decision_type, result, payload)
    except Exception as exc:
        trace_logging.log_info(
            "agent_llm_output_validation_failed",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
            error=trace_logging.snippet(str(exc), limit=300),
            output_keys=sorted(str(key) for key in result.keys()),
        )
        return safe_fallback_output(decision_type, payload, exc)
