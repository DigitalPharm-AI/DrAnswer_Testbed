from __future__ import annotations

from typing import Any

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError
from agent_app.llm.messages import patient_summary_with_source
from agent_app.llm.responses import (
    finalized_chat_summary,
    natural_chat_summary,
)
from agent_app.llm.validation import validate_llm_output
from shared.redaction import safe_exception_summary

MULTITURN_CHAT_AGENT_NAME = "multiturn_chat_agent"


def required_llm_summary(
    message: Any,
    output: dict[str, Any],
    *,
    trace_id: str,
    decision_type: str,
    finalized: bool = False,
) -> tuple[str, str]:
    if finalized:
        summary = finalized_chat_summary(output)
        source = str(output.get("fallback") or "model_output")
        if not summary:
            summary, source = patient_summary_with_source(
                message,
                "",
                fallback_source="model_output",
            )
    else:
        summary, source = patient_summary_with_source(
            message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
    summary = summary.strip()
    if not summary:
        raise AgentExecutionError(
            "llm_final_answer_missing",
            error_type="llm_final_answer_missing",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type=decision_type,
        )
    return summary, source


def normalize_mutation_confirmation_output(
    output: dict[str, Any],
) -> dict[str, Any]:
    """Project provider wording onto the canonical message field."""

    normalized = dict(output)
    message = str(normalized.get("message") or "").strip()
    if not message:
        question = str(normalized.get("question") or "").strip()
        if question:
            normalized["message"] = question
        else:
            advice = str(normalized.get("advice") or "").strip()
            if advice:
                normalized["message"] = advice
    normalized.pop("question", None)
    normalized.pop("advice", None)
    return normalized


def validate_multiturn_output(
    trace_id: str,
    output: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    try:
        validate_llm_output("system_guidance", output, payload)
    except Exception as exc:
        trace_logging.log_info(
            "agent_llm_output_validation_failed",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
            error=safe_exception_summary(exc, limit=300),
            output_keys=sorted(str(key) for key in output),
        )
        raise AgentExecutionError(
            str(exc),
            error_type="llm_output_validation_failed",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
        ) from exc
