from __future__ import annotations

import asyncio
from typing import Any

from agent_app.agents.nutrition_management import NutritionManagementAgent
from agent_app.tool_runtime import ToolRuntime
from tests.test_agent_app_langgraph_native import NativeChatProvider


class ConcurrentSpecialistProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0)
        return {"message": f"전문 응답: {user_payload['message']}"}


def test_reused_specialist_graph_keeps_concurrent_invocation_state_isolated():
    provider = ConcurrentSpecialistProvider()
    agent = NutritionManagementAgent(provider, ToolRuntime(None))
    graph_id = id(agent.graph_runner.graph)

    async def invoke_all():
        return await asyncio.gather(
            agent.run("trace-first", {"message": "첫 번째 요청", "context": {}}),
            agent.run("trace-second", {"message": "두 번째 요청", "context": {}}),
            agent.run(
                "trace-forced",
                {"message": "강제 호출 없는 요청", "context": {}},
                forced_tool_calls=[],
            ),
        )

    first, second, forced = asyncio.run(invoke_all())

    assert id(agent.graph_runner.graph) == graph_id
    assert first.trace_id == "trace-first"
    assert second.trace_id == "trace-second"
    assert forced.trace_id == "trace-forced"
    assert first.human_summary == "전문 응답: 첫 번째 요청"
    assert second.human_summary == "전문 응답: 두 번째 요청"
    assert forced.human_summary == "강제 호출 없는 요청"
    assert len(provider.chat_model_bound_tool_history) == 2
    assert all(history for history in provider.chat_model_bound_tool_history)
