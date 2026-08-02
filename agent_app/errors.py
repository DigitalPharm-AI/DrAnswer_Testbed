from __future__ import annotations

from dataclasses import dataclass


class AgentExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        error_type: str,
        trace_id: str,
        agent_name: str,
        decision_type: str,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.trace_id = trace_id
        self.agent_name = agent_name
        self.decision_type = decision_type
        self.retryable = retryable


@dataclass(frozen=True)
class PublicProcessingError:
    code: str
    message: str
    retryable: bool


_PUBLIC_AGENT_ERRORS: dict[str, PublicProcessingError] = {
    "llm_output_parse_failed": PublicProcessingError(
        code="LLM_OUTPUT_PARSE_FAILED",
        message="The LLM response could not be parsed as the required JSON object.",
        retryable=True,
    ),
    "llm_tool_arguments_parse_failed": PublicProcessingError(
        code="LLM_TOOL_ARGUMENTS_INVALID",
        message="The LLM returned invalid Tool arguments.",
        retryable=True,
    ),
    "llm_output_validation_failed": PublicProcessingError(
        code="LLM_OUTPUT_VALIDATION_FAILED",
        message="The LLM response did not satisfy the required schema.",
        retryable=True,
    ),
    "llm_final_answer_missing": PublicProcessingError(
        code="LLM_FINAL_ANSWER_MISSING",
        message="The LLM did not return a final answer.",
        retryable=True,
    ),
    "provider_request_failed": PublicProcessingError(
        code="LLM_PROVIDER_REQUEST_FAILED",
        message="The LLM provider request failed.",
        retryable=True,
    ),
    "mutation_tool_not_applied": PublicProcessingError(
        code="TOOL_EXECUTION_FAILED",
        message="The requested Tool operation could not be completed.",
        retryable=True,
    ),
    "payload_validation_failed": PublicProcessingError(
        code="AI_PAYLOAD_VALIDATION_FAILED",
        message="The AI processing payload did not satisfy the required schema.",
        retryable=False,
    ),
    "approved_write_followup_tool_forbidden": PublicProcessingError(
        code="LLM_OUTPUT_VALIDATION_FAILED",
        message="The LLM response did not satisfy the required schema.",
        retryable=False,
    ),
    "tool_execution_budget_exceeded": PublicProcessingError(
        code="TOOL_EXECUTION_BUDGET_EXCEEDED",
        message=("The AI request reached its Tool execution limit. Split the request into smaller steps and try again."),
        retryable=False,
    ),
    "pro_ctcae_survey_response_invalid": PublicProcessingError(
        code="PRO_CTCAE_RESPONSE_INVALID",
        message=("The questionnaire response is not one of the allowed options."),
        retryable=False,
    ),
    "pro_ctcae_survey_stale_response": PublicProcessingError(
        code="PRO_CTCAE_STALE_RESPONSE",
        message=("The questionnaire response does not belong to the current question."),
        retryable=False,
    ),
    "pro_ctcae_survey_expired": PublicProcessingError(
        code="PRO_CTCAE_SURVEY_EXPIRED",
        message="The questionnaire session has expired.",
        retryable=False,
    ),
    "pro_ctcae_survey_state_failed": PublicProcessingError(
        code="PRO_CTCAE_SURVEY_STATE_FAILED",
        message="The questionnaire state could not be processed.",
        retryable=True,
    ),
    "missed_dose_policy_context_invalid": PublicProcessingError(
        code="MISSED_DOSE_POLICY_CONTEXT_INVALID",
        message="The missed-dose policy context is invalid or incomplete.",
        retryable=False,
    ),
}


def public_processing_error(exc: Exception) -> PublicProcessingError:
    """Map an internal failure to a stable, non-sensitive public error."""

    if isinstance(exc, TimeoutError):
        return PublicProcessingError(
            code="AI_PROCESSING_TIMEOUT",
            message="The AI request exceeded the processing time limit.",
            retryable=bool(getattr(exc, "retryable", True)),
        )
    if isinstance(exc, AgentExecutionError):
        mapped = _PUBLIC_AGENT_ERRORS.get(exc.error_type)
        if mapped is not None:
            return PublicProcessingError(
                code=mapped.code,
                message=mapped.message,
                retryable=(
                    mapped.retryable
                    if exc.retryable is None
                    else exc.retryable
                ),
            )
    retryable_override = getattr(exc, "retryable", None)
    return PublicProcessingError(
        code="AI_PROCESSING_ERROR",
        message="An internal AI Server processing error occurred.",
        retryable=(
            True
            if retryable_override is None
            else bool(retryable_override)
        ),
    )
