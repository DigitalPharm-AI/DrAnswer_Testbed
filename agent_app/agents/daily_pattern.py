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
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID, agent_error
from agent_app.llm.messages import patient_summary_with_source
from agent_app.llm.prompts import daily_pattern_final_prompt, daily_pattern_prompt
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.policy import has_deferred_policy_tool_call
from agent_app.tools.results import tool_calls_payload
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse, DailyMedicationPattern
from shared.tool_names import PROPOSE_NOTIFICATION_POLICY


class DailyPatternAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.graph_runner = SingleRoundToolAgentGraph(
            provider=provider,
            tool_runtime=tool_runtime,
            agent_name="daily_pattern_agent",
            validation_decision_type="pattern_analysis",
            source_event_type="daily_pattern",
            prompt=daily_pattern_prompt(),
            final_prompt=daily_pattern_final_prompt(),
            response_mode="daily_pattern_analysis",
            tool_names=(PROPOSE_NOTIFICATION_POLICY,),
            response_builder=self._build_response,
        )

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        try:
            pattern = DailyMedicationPattern.model_validate(payload)
        except Exception as exc:
            raise agent_error(trace_id, "daily_pattern_agent", "pattern_policy_recommendation", exc) from exc
        return await self.graph_runner.invoke(trace_id, pattern.model_dump(mode="json"))

    @staticmethod
    def _build_response(state: SingleRoundAgentGraphState) -> AgentResponse:
        executed_calls = state.get("executed_calls", [])
        results = state.get("tool_results", [])
        decision_output = state.get("decision_model_output", {})
        final_output = state.get("final_model_output", {})
        response_output = final_output if executed_calls else decision_output
        response_message = state.get("final_ai_message") if executed_calls else state["decision_ai_message"]
        policy_confirmation_required = has_deferred_policy_tool_call(executed_calls)
        generated_summary = str(response_output.get("message") or response_output.get("summary") or "").strip()
        human_summary, final_answer_source = patient_summary_with_source(
            response_message,
            generated_summary,
            fallback_source="model_output",
        )
        human_summary = human_summary.strip()
        if not human_summary:
            raise AgentExecutionError(
                "llm_final_answer_missing",
                error_type="llm_final_answer_missing",
                trace_id=state["trace_id"],
                agent_name="daily_pattern_agent",
                decision_type="tool_call" if executed_calls else "pattern_policy_recommendation",
            )
        structured_payload = {
            "summary": str(response_output.get("summary") or response_output.get("message") or ""),
            "analysis": response_output,
            "model_output": decision_output,
            **tool_calls_payload(executed_calls, results),
            "policy_confirmation_required": policy_confirmation_required,
            "agent_graph_mode": AGENT_GRAPH_MODE,
            "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
            "message_flow": single_round_message_flow(len(results)),
            "final_answer_source": final_answer_source,
        }
        if executed_calls:
            structured_payload["final_model_output"] = final_output
            structured_payload["finalization_mode"] = LLM_AFTER_TOOL_FINALIZATION_MODE
        return AgentResponse(
            trace_id=state["trace_id"],
            agent_name="daily_pattern_agent",
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="tool_call" if executed_calls else "pattern_policy_recommendation",
            structured_payload=structured_payload,
            human_summary=human_summary,
            requires_conversation_alert=False,
        )
