from __future__ import annotations

from typing import Any

from agent_app.generation import PROMPT_VERSION_ID, agent_error, generate_llm_output
from agent_app.prompt_builders import daily_pattern_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.tool_calling import normalize_tool_calls
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_names import PROPOSE_NOTIFICATION_POLICY
from agent_app.tool_policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse, DailyMedicationPattern


class DailyPatternAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        agent_name = "daily_pattern_agent"
        decision_type = "pattern_policy_recommendation"
        try:
            pattern = DailyMedicationPattern.model_validate(payload)
            output = await generate_llm_output(
                self.provider,
                trace_id,
                agent_name,
                "pattern_analysis",
                daily_pattern_prompt(),
                {
                    **pattern.model_dump(mode="json"),
                    "response_mode": "daily_pattern_analysis",
                    "available_tools": ToolCatalog.tools_for(PROPOSE_NOTIFICATION_POLICY),
                },
            )
            tool_calls = normalize_policy_tool_calls(normalize_tool_calls(output), source_event_type="daily_pattern")
            executed_calls, results = await self.tool_runtime.execute(
                tool_calls,
                trace_id=trace_id,
                source_event_type="daily_pattern",
                payload=pattern.model_dump(mode="json"),
            )
        except Exception as exc:
            raise agent_error(trace_id, agent_name, decision_type, exc) from exc

        policy_confirmation_required = has_deferred_policy_tool_call(executed_calls)
        structured_payload = {
            "summary": str(output.get("summary") or output.get("message") or ""),
            "analysis": output,
            **tool_calls_payload(executed_calls, results),
            "policy_confirmation_required": policy_confirmation_required,
        }
        fallback_summary = str(output.get("message") or output.get("summary") or "")
        if policy_confirmation_required and not fallback_summary:
            fallback_summary = "알림 정책 변경 후보를 만들었습니다. 확인 후 반영합니다."
        human_summary = tool_result_summary(results, fallback_summary or "오늘 복약 패턴을 분석했습니다.")
        return AgentResponse(
            trace_id=trace_id,
            agent_name=agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="tool_call" if executed_calls else decision_type,
            structured_payload=structured_payload,
            human_summary=human_summary,
            requires_conversation_alert=False,
        )
