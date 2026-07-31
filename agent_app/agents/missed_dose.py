from __future__ import annotations

from time import perf_counter
from typing import Any

from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import agent_error
from agent_app.llm.messages import (
    build_chat_messages,
    model_output_from_ai_message,
    tool_calls_from_ai_message,
)
from agent_app.llm.prompts import missed_dose_generation_prompt
from agent_app.llm.validation import (
    validate_missed_dose_generation_output,
)
from agent_app.observability.model_calls import traced_model_ainvoke
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse, MissedDoseEventPayload

MISSED_DOSE_FEEDBACK_MODE = "llm_policy_bounded_generation"
MISSED_DOSE_PROMPT_VERSION_ID = "missed_dose_generation_v1"
ALLOWED_PATTERN_CODES = {"A", "B", "C", "D", "E"}
ALLOWED_TONE_KEYS = {
    "empathy",
    "persuasion",
    "practical",
    "warning_soft",
    "side_effect_check",
}
FORBIDDEN_MESSAGE_TERMS = (
    "타목시펜",
    "레트로졸",
    "암",
    "당뇨",
    "고혈압",
    "항암",
    "병기",
    "재발 위험",
    "치료 실패",
    "생명",
    "위험",
    "큰일",
    "반드시",
    "무조건",
    "즉시",
    "당장",
    "복용하세요",
    "드세요",
    "먹으세요",
)


class MissedDoseAgent:
    """Generate one policy-bounded missed-dose message without Tool calling.

    The asynchronous intake has no patient-authored symptom. Side-effect and
    PRO-CTCAE Tools therefore remain in the synchronous medication-chat path,
    after the patient has replied. The Backend remains authoritative for
    notification eligibility, A-E pattern, tone policy, and final sending.
    """

    def __init__(
        self,
        provider: BaseLLMProvider,
        tool_runtime: ToolRuntime,
    ) -> None:
        self.provider = provider
        # Kept for the shared orchestrator contract. This workflow deliberately
        # exposes no Tools to the model.
        self.tool_runtime = tool_runtime

    async def run(
        self,
        trace_id: str,
        payload: dict[str, Any],
    ) -> AgentResponse:
        try:
            event = MissedDoseEventPayload.model_validate(payload)
        except Exception as exc:
            raise agent_error(
                trace_id,
                "missed_dose_coach",
                "missed_dose_assessment",
                exc,
            ) from exc

        pattern_context = dict(event.adherence_pattern_context)
        tone_context = dict(event.tone_policy_context)
        try:
            pattern_code, tone_key = _validated_generation_policy(
                pattern_context=pattern_context,
                tone_context=tone_context,
            )
        except ValueError as exc:
            raise AgentExecutionError(
                str(exc),
                error_type="missed_dose_policy_context_invalid",
                trace_id=trace_id,
                agent_name="missed_dose_coach",
                decision_type="missed_dose_assessment",
            ) from exc

        generation_payload = _generation_payload(
            event,
            pattern_context=pattern_context,
            tone_context=tone_context,
            pattern_code=pattern_code,
            tone_key=tone_key,
        )
        started = perf_counter()
        try:
            ai_message = await traced_model_ainvoke(
                self.provider.chat_model(),
                build_chat_messages(
                    missed_dose_generation_prompt(),
                    generation_payload,
                ),
                name="missed_dose_generation",
                prompt_version_id=MISSED_DOSE_PROMPT_VERSION_ID,
            )
        except Exception as exc:
            raise agent_error(
                trace_id,
                "missed_dose_coach",
                "missed_dose_assessment",
                exc,
            ) from exc

        try:
            if tool_calls_from_ai_message(ai_message):
                raise ValueError("missed_dose_tool_call_forbidden")
            model_output = model_output_from_ai_message(ai_message)
            validated_output = validate_missed_dose_generation_output(
                model_output
            )
            message = str(
                validated_output.get("generated_message") or ""
            ).strip()
            message_errors = _feedback_message_errors(
                message,
                medication_name=event.medication_name,
            )
            if message_errors:
                raise ValueError(
                    "missed_dose_generated_message_invalid:"
                    + ",".join(message_errors)
                )
        except AgentExecutionError:
            raise
        except Exception as exc:
            converted = agent_error(
                trace_id,
                "missed_dose_coach",
                "missed_dose_assessment",
                exc,
            )
            if converted.error_type in {
                "llm_output_parse_failed",
                "llm_tool_arguments_parse_failed",
            }:
                raise converted from exc
            raise AgentExecutionError(
                str(exc),
                error_type="llm_output_validation_failed",
                trace_id=trace_id,
                agent_name="missed_dose_coach",
                decision_type="missed_dose_assessment",
            ) from exc

        elapsed_ms = round((perf_counter() - started) * 1000)
        return AgentResponse(
            trace_id=trace_id,
            agent_name="missed_dose_coach",
            prompt_version_id=MISSED_DOSE_PROMPT_VERSION_ID,
            decision_type="missed_dose_assessment",
            structured_payload={
                "dose_event_id": event.dose_event_id,
                "feedback_category": pattern_code,
                "feedback_tone": tone_key,
                "adherence_pattern_context": pattern_context,
                "tone_policy_context": tone_context,
                "missed_dose_hybrid": {
                    "reason": str(pattern_context.get("reason") or ""),
                    "generated_message": message,
                    "pattern_code": pattern_code,
                    "tone_key": tone_key,
                    "safety_notes": [
                        "llm_generated",
                        "no_medication_name",
                        "no_diagnosis",
                        "non_directive",
                        "schema_validated",
                    ],
                },
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "routing_mode": "llm_policy_bounded_generation",
                "executed_by": "missed_dose_coach",
                "agent_graph_mode": MISSED_DOSE_FEEDBACK_MODE,
                "tool_execution_mode": "none",
                "iterations": 1,
                "message_flow": [
                    "BackendReadPolicyContext",
                    "HumanMessage",
                    "AIMessage(structured_output)",
                    "AgentResponse",
                ],
                "model_output": model_output,
                "final_answer_source": "llm_structured_output",
                "elapsed_ms": elapsed_ms,
            },
            human_summary=message,
            requires_conversation_alert=True,
        )


def _validated_generation_policy(
    *,
    pattern_context: dict[str, Any],
    tone_context: dict[str, Any],
) -> tuple[str, str]:
    pattern_code = str(pattern_context.get("pattern_code") or "").upper()
    if pattern_code not in ALLOWED_PATTERN_CODES:
        raise ValueError(
            f"unsupported_missed_dose_pattern_code:{pattern_code or 'missing'}"
        )

    tone_pattern_code = str(
        tone_context.get("pattern_code") or ""
    ).upper()
    if tone_pattern_code != pattern_code:
        raise ValueError(
            "missed_dose_pattern_tone_context_mismatch:"
            f"{pattern_code}:{tone_pattern_code or 'missing'}"
        )

    tone_key = str(tone_context.get("tone_key") or "")
    if tone_key not in ALLOWED_TONE_KEYS:
        raise ValueError(
            f"unsupported_missed_dose_tone_key:{tone_key or 'missing'}"
        )

    return pattern_code, tone_key


def _generation_payload(
    event: MissedDoseEventPayload,
    *,
    pattern_context: dict[str, Any],
    tone_context: dict[str, Any],
    pattern_code: str,
    tone_key: str,
) -> dict[str, Any]:
    """Project only the policy evidence needed to write the message."""

    return {
        "response_mode": "missed_dose_message_generation",
        "decision_type": "missed_dose_message_generation",
        "scheduled_slot": event.slot_label,
        "scheduled_for": event.scheduled_for.isoformat(),
        "detected_at": event.detected_at.isoformat(),
        "policy": {
            "pattern_code": pattern_code,
            "pattern_label": str(
                pattern_context.get("pattern_label") or ""
            ),
            "pattern_reason": str(
                pattern_context.get("reason") or ""
            ),
            "tone_key": tone_key,
            "tone_selection_reason": str(
                tone_context.get("selection_reason") or ""
            ),
        },
    }


def _feedback_message_errors(
    message: str,
    *,
    medication_name: str,
) -> list[str]:
    errors: list[str] = []
    if not message:
        errors.append("message_empty")
    forbidden_terms = set(FORBIDDEN_MESSAGE_TERMS)
    if medication_name.strip():
        forbidden_terms.add(medication_name.strip())
    for term in sorted(forbidden_terms):
        if term and term in message:
            errors.append(f"forbidden_term:{term}")
    if message.count("!") + message.count("?") > 1:
        errors.append("too_many_punctuation_marks")
    if len(message) > 45:
        errors.append("message_too_long")
    return errors
