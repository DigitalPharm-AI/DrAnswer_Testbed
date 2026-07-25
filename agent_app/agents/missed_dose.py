from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import (
    ITERATIVE_FINALIZATION_MODE,
    ITERATIVE_TOOL_EXECUTION_MODE,
    TOOL_LOOP_MODE,
    ToolChatAgentGraph,
    ToolChatGraphState,
    tool_chat_message_flow,
)
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID, agent_error
from agent_app.llm.prompts import missed_dose_prompt
from agent_app.providers.base import BaseLLMProvider
from agent_app.llm.responses import missed_dose_hybrid_payload, string_list, text_field
from agent_app.tools.names import (
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    SOURCE_MISSED_DOSE,
)
from agent_app.tools.results import tool_calls_payload
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse, MissedDoseEventPayload


class MissedDoseAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.graph_runner = ToolChatAgentGraph(
            provider=provider,
            tool_runtime=tool_runtime,
            agent_name="missed_dose_coach",
            prompt=missed_dose_prompt(),
            response_mode="missed_dose_coaching",
            decision_type="missed_dose_assessment",
            tool_names=(GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,),
            source_event_type=SOURCE_MISSED_DOSE,
            force_ae_after_positive_lookup=True,
            defer_async_continuation=False,
            validation_decision_type="missed_dose_assessment",
            response_builder=self._build_response,
            requires_conversation_alert=True,
        )

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        try:
            event = MissedDoseEventPayload.model_validate(payload)
        except Exception as exc:
            raise agent_error(trace_id, "missed_dose_coach", "missed_dose_assessment", exc) from exc
        return await self.graph_runner.invoke(trace_id, event.model_dump(mode="json"))

    @staticmethod
    def _build_response(state: ToolChatGraphState) -> AgentResponse:
        event = MissedDoseEventPayload.model_validate(state["request_payload"])
        executed_calls = state.get("all_executed_calls", [])
        results = state.get("all_results", [])
        decision_output = state.get("initial_model_output", {})
        response_output = state.get("current_model_output", {})
        questions = string_list(response_output.get("follow_up_questions")) or ["현재 복용 가능한 상태인지 알려주세요."]
        side_effect_signal = bool(response_output.get("side_effect_signal")) or any(
            result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT and result.status == "success" and result.response.get("suspected") for result in results
        )
        hybrid_payload = missed_dose_hybrid_payload(response_output)
        structured_payload = {
            "dose_event_id": event.dose_event_id,
            "likely_reason": str(response_output.get("likely_reason") or "unknown"),
            "side_effect_signal": side_effect_signal,
            "side_effect_status": "suspected" if side_effect_signal else "not_assessed",
            "symptom_summary": str(response_output.get("symptom_summary") or ""),
            "follow_up_questions": questions,
            "recommendation": str(response_output.get("recommendation") or ""),
            "missed_dose_hybrid": hybrid_payload,
            "model_output": decision_output,
            **tool_calls_payload(executed_calls, results),
            "routing_mode": "event_tool" if executed_calls else "event_answer",
            "executed_by": "missed_dose_coach",
            "agent_graph_mode": TOOL_LOOP_MODE,
            "tool_loop_mode": TOOL_LOOP_MODE,
            "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
            "iterations": state.get("iterations", 0),
            "message_flow": tool_chat_message_flow(len(results)),
        }
        if executed_calls:
            structured_payload["final_model_output"] = response_output
            structured_payload["finalization_mode"] = ITERATIVE_FINALIZATION_MODE
        human_summary = text_field(hybrid_payload.get("generated_message"))
        final_answer_source = "missed_dose_hybrid.generated_message"
        if not human_summary:
            human_summary = text_field(response_output.get("patient_message")) or text_field(response_output.get("message"))
            final_answer_source = "model_output"
        if not human_summary:
            raise AgentExecutionError(
                "missed_dose_patient_message_missing",
                error_type="llm_output_validation_failed",
                trace_id=state["trace_id"],
                agent_name="missed_dose_coach",
                decision_type="missed_dose_assessment",
            )
        structured_payload["final_answer_source"] = final_answer_source
        return AgentResponse(
            trace_id=state["trace_id"],
            agent_name="missed_dose_coach",
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="side_effect_assessment" if executed_calls else "missed_dose_assessment",
            structured_payload=structured_payload,
            human_summary=human_summary,
            requires_conversation_alert=True,
        )
