from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.llm.prompts import medication_agent_prompt
from agent_app.providers.base import BaseLLMProvider
from shared.tool_names import SOURCE_MEDICATION_AGENT
from shared.tool_permissions import MEDICATION_CHAT_TOOLS
from agent_app.tools.runtime import ToolRuntime
from agent_app.tools.policy_gate import ToolCallOrigin
from shared.schemas import AgentResponse


class MedicationAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.graph_runner = ToolChatAgentGraph(
            provider=self.provider,
            tool_runtime=self.tool_runtime,
            agent_name="medication_agent",
            prompt=medication_agent_prompt(),
            response_mode="medication_chat",
            decision_type="tool_call",
            tool_names=tuple(MEDICATION_CHAT_TOOLS),
            source_event_type=SOURCE_MEDICATION_AGENT,
            force_ae_after_positive_lookup=True,
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
        origin: ToolCallOrigin = ToolCallOrigin.CLINICAL_CONTINUATION,
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
