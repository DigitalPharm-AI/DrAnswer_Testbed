from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import run_tool_chat_agent
from agent_app.prompt_builders import medication_agent_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.tool_permissions import MEDICATION_CHAT_TOOLS
from agent_app.tool_names import SOURCE_MEDICATION_AGENT
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse


class MedicationAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime

    async def run(self, trace_id: str, request_payload: dict[str, Any], *, forced_tool_calls: list[dict[str, Any]] | None = None) -> AgentResponse:
        return await run_tool_chat_agent(
            provider=self.provider,
            tool_runtime=self.tool_runtime,
            trace_id=trace_id,
            request_payload=request_payload,
            agent_name="medication_agent",
            prompt=medication_agent_prompt(),
            response_mode="medication_chat",
            decision_type="tool_call",
            tool_names=tuple(MEDICATION_CHAT_TOOLS),
            source_event_type=SOURCE_MEDICATION_AGENT,
            forced_tool_calls=forced_tool_calls,
            force_ae_after_positive_lookup=True,
        )
