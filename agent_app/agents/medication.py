from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.prompt_builders import medication_agent_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.tool_names import SOURCE_MEDICATION_AGENT
from agent_app.tool_permissions import MEDICATION_CHAT_TOOLS
from agent_app.tool_runtime import ToolRuntime
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

    async def run(self, trace_id: str, request_payload: dict[str, Any], *, forced_tool_calls: list[dict[str, Any]] | None = None) -> AgentResponse:
        return await self.graph_runner.invoke(trace_id, request_payload, forced_tool_calls=forced_tool_calls)
