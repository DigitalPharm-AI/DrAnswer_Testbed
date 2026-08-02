from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.llm.prompts import nutrition_management_agent_prompt
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.policy_gate import ToolCallOrigin
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse
from shared.tool_names import SOURCE_NUTRITION_MANAGEMENT_AGENT
from shared.tool_permissions import NUTRITION_MANAGEMENT_TOOLS


class NutritionManagementAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.graph_runner = ToolChatAgentGraph(
            provider=self.provider,
            tool_runtime=self.tool_runtime,
            agent_name="nutrition_management_agent",
            prompt=nutrition_management_agent_prompt(),
            response_mode="nutrition_management_chat",
            decision_type="tool_call",
            tool_names=tuple(NUTRITION_MANAGEMENT_TOOLS),
            source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
        )

    async def run(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
    ) -> AgentResponse:
        return await self.graph_runner.invoke(trace_id, request_payload)

    async def continue_with_tool_calls(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_calls: list[dict[str, Any]],
        origin: ToolCallOrigin = (
            ToolCallOrigin.CLINICAL_CONTINUATION
        ),
    ) -> AgentResponse:
        return await self.graph_runner.continue_with_tool_calls(
            trace_id,
            request_payload,
            tool_calls=tool_calls,
            origin=origin,
        )

    async def execute_approved_write(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_call: dict[str, Any],
    ) -> AgentResponse:
        return await self.graph_runner.execute_approved_write(
            trace_id,
            request_payload,
            tool_call=tool_call,
        )
