from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import run_tool_chat_agent
from agent_app.prompt_builders import nutrition_recommendation_agent_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.tool_permissions import NUTRITION_RECOMMENDATION_TOOLS
from agent_app.tool_names import SOURCE_NUTRITION_RECOMMENDATION_AGENT
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse


class NutritionRecommendationAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime

    async def run(self, trace_id: str, request_payload: dict[str, Any], *, forced_tool_calls: list[dict[str, Any]] | None = None) -> AgentResponse:
        return await run_tool_chat_agent(
            provider=self.provider,
            tool_runtime=self.tool_runtime,
            trace_id=trace_id,
            request_payload=request_payload,
            agent_name="nutrition_recommendation_agent",
            prompt=nutrition_recommendation_agent_prompt(),
            response_mode="nutrition_recommendation_chat",
            decision_type="tool_call",
            tool_names=tuple(NUTRITION_RECOMMENDATION_TOOLS),
            source_event_type=SOURCE_NUTRITION_RECOMMENDATION_AGENT,
            forced_tool_calls=forced_tool_calls,
        )
