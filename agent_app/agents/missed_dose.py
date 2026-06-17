from __future__ import annotations

from typing import Any

from agent_app.generation import PROMPT_VERSION_ID, agent_error, generate_llm_output
from agent_app.prompt_builders import missed_dose_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.response_builders import missed_dose_hybrid_payload, string_list
from agent_app.tool_calling import normalize_tool_calls
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse, MissedDoseEventPayload


class MissedDoseAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        agent_name = "missed_dose_coach"
        decision_type = "missed_dose_assessment"
        try:
            event = MissedDoseEventPayload.model_validate(payload)
            event_payload = event.model_dump(mode="json")
            output = await generate_llm_output(
                self.provider,
                trace_id,
                agent_name,
                decision_type,
                missed_dose_prompt(),
                {
                    **event_payload,
                    "response_mode": "missed_dose_coaching",
                    "available_tools": ToolCatalog.tools_for("lookup_side_effect_info", "AE_pro_ctcae"),
                },
            )
            tool_calls = normalize_tool_calls(output)
            executed_calls, results = await self.tool_runtime.execute(tool_calls, trace_id=trace_id, source_event_type="missed_dose", payload=event_payload)
        except Exception as exc:
            raise agent_error(trace_id, agent_name, decision_type, exc) from exc

        questions = string_list(output.get("follow_up_questions")) or ["현재 복용 가능한 상태인지 알려주세요."]
        side_effect_signal = bool(output.get("side_effect_signal")) or any(
            result.tool_name == "lookup_side_effect_info" and result.status == "success" and result.response.get("suspected") for result in results
        )
        structured_payload = {
            "dose_event_id": event.dose_event_id,
            "likely_reason": str(output.get("likely_reason") or "unknown"),
            "side_effect_signal": side_effect_signal,
            "side_effect_status": "suspected" if side_effect_signal else "not_assessed",
            "symptom_summary": str(output.get("symptom_summary") or ""),
            "follow_up_questions": questions,
            "recommendation": str(output.get("recommendation") or ""),
            "missed_dose_hybrid": missed_dose_hybrid_payload(output),
            "model_output": output,
            **tool_calls_payload(executed_calls, results),
        }
        human_summary = tool_result_summary(
            results,
            str(output.get("patient_message") or output.get("message") or f"{event.slot_label} 복약이 놓친 것으로 확인됐습니다. 현재 상태를 알려주세요."),
        )
        return AgentResponse(
            trace_id=trace_id,
            agent_name=agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="side_effect_assessment" if executed_calls else decision_type,
            structured_payload=structured_payload,
            human_summary=human_summary,
            requires_conversation_alert=True,
        )
