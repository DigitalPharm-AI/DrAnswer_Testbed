from __future__ import annotations

from typing import Any

from agent_app.agents.single_round_graph import (
    AGENT_GRAPH_MODE,
    LLM_AFTER_TOOL_FINALIZATION_MODE,
    SINGLE_ROUND_TOOL_EXECUTION_MODE,
    SingleRoundAgentGraphState,
    SingleRoundToolAgentGraph,
    single_round_message_flow,
)
from agent_app.chat_tooling import patient_summary_with_source
from agent_app.generation import PROMPT_VERSION_ID, agent_error
from agent_app.prompt_builders import missed_dose_final_prompt, missed_dose_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.response_builders import missed_dose_hybrid_payload, string_list
from agent_app.tool_names import GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse, MissedDoseEventPayload


class MissedDoseAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.graph_runner = SingleRoundToolAgentGraph(
            provider=provider,
            tool_runtime=tool_runtime,
            agent_name="missed_dose_coach",
            validation_decision_type="missed_dose_assessment",
            source_event_type="missed_dose",
            prompt=missed_dose_prompt(),
            final_prompt=missed_dose_final_prompt(),
            response_mode="missed_dose_coaching",
            tool_names=(GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE),
            response_builder=self._build_response,
        )

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        try:
            event = MissedDoseEventPayload.model_validate(payload)
        except Exception as exc:
            raise agent_error(trace_id, "missed_dose_coach", "missed_dose_assessment", exc) from exc
        return await self.graph_runner.invoke(trace_id, event.model_dump(mode="json"))

    @staticmethod
    def _build_response(state: SingleRoundAgentGraphState) -> AgentResponse:
        event = MissedDoseEventPayload.model_validate(state["request_payload"])
        executed_calls = state.get("executed_calls", [])
        results = state.get("tool_results", [])
        decision_output = state.get("decision_model_output", {})
        final_output = state.get("final_model_output", {})
        response_output = final_output if executed_calls else decision_output
        response_message = state.get("final_ai_message") if executed_calls else state["decision_ai_message"]
        questions = string_list(response_output.get("follow_up_questions")) or ["현재 복용 가능한 상태인지 알려주세요."]
        side_effect_signal = bool(response_output.get("side_effect_signal")) or any(
            result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT
            and result.status == "success"
            and result.response.get("suspected")
            for result in results
        )
        structured_payload = {
            "dose_event_id": event.dose_event_id,
            "likely_reason": str(response_output.get("likely_reason") or "unknown"),
            "side_effect_signal": side_effect_signal,
            "side_effect_status": "suspected" if side_effect_signal else "not_assessed",
            "symptom_summary": str(response_output.get("symptom_summary") or ""),
            "follow_up_questions": questions,
            "recommendation": str(response_output.get("recommendation") or ""),
            "missed_dose_hybrid": missed_dose_hybrid_payload(response_output),
            "model_output": decision_output,
            **tool_calls_payload(executed_calls, results),
            "agent_graph_mode": AGENT_GRAPH_MODE,
            "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
            "message_flow": single_round_message_flow(len(results)),
        }
        if executed_calls:
            structured_payload["final_model_output"] = final_output
            structured_payload["finalization_mode"] = LLM_AFTER_TOOL_FINALIZATION_MODE
        fallback_summary = tool_result_summary(
            results,
            str(
                response_output.get("patient_message")
                or response_output.get("message")
                or f"{event.slot_label} 복약을 놓친 것으로 확인했어요. 현재 상태를 알려주세요."
            ),
        )
        human_summary, final_answer_source = patient_summary_with_source(
            response_message,
            fallback_summary,
            fallback_source="tool_result_summary" if results else "model_output",
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
