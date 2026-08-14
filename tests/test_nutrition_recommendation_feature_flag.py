from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

import pytest

from agent_app.errors import AgentExecutionError
from agent_app.orchestration.delegation import delegation_tools_payload
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from shared.schemas import MultiturnChatRequest
from shared.tool_names import (
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
)
from tests.support.agent_scenarios import NativeFakeToolExecutor
from tests.support.llm import NativeChatProvider


class RecommendationFeatureProvider(NativeChatProvider):
    def __init__(self, *, forced_tool_name: str | None = None) -> None:
        self.forced_tool_name = forced_tool_name
        self.seen_prompts: list[str] = []
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.seen_prompts.append(system_prompt)
        if self.forced_tool_name:
            return {
                "tool_call": {
                    "name": self.forced_tool_name,
                    "arguments": {
                        "task": "Recommend dinner.",
                        "reason": "forced disabled-tool regression case",
                    },
                }
            }
        return {"message": "저녁은 채소와 단백질을 곁들여 가볍게 구성해 보세요."}


def _recommendation_request() -> MultiturnChatRequest:
    return MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="오늘 저녁에는 무엇을 먹는 게 좋을까?",
        current_time=datetime(2026, 8, 14, 18, 0),
        context={},
    )


def _tool_names(*, recommendation_enabled: bool) -> set[str]:
    return {
        str(tool["name"])
        for tool in delegation_tools_payload(
            nutrition_recommendation_enabled=recommendation_enabled,
        )
    }


def test_disabled_recommendation_delegation_is_removed_from_catalog():
    tool_names = _tool_names(recommendation_enabled=False)

    assert DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT in tool_names
    assert DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT not in tool_names


def test_disabled_recommendation_is_answered_directly_without_specialist_tool():
    provider = RecommendationFeatureProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider,
        NativeFakeToolExecutor(),
        nutrition_recommendation_enabled=False,
    )

    response = asyncio.run(
        orchestrator.invoke(
            "multiturn_chat",
            _recommendation_request().model_dump(mode="json"),
        )
    )

    bound_tools = provider.chat_model_bound_tool_history[0]
    assert DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT in bound_tools
    assert DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT not in bound_tools
    assert DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT not in provider.seen_prompts[0]
    assert "answer directly in Korean using general knowledge" in provider.seen_prompts[0]
    assert response.structured_payload["routing_mode"] == "direct_answer"
    assert response.structured_payload["supervisor_tool_calls"] == []


def test_enabled_recommendation_keeps_existing_specialist_tool_contract():
    provider = RecommendationFeatureProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider,
        NativeFakeToolExecutor(),
        nutrition_recommendation_enabled=True,
    )

    asyncio.run(
        orchestrator.invoke(
            "multiturn_chat",
            _recommendation_request().model_dump(mode="json"),
        )
    )

    assert DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT in provider.chat_model_bound_tool_history[0]
    assert DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT in provider.seen_prompts[0]


def test_disabled_recommendation_rejects_injected_specialist_call():
    provider = RecommendationFeatureProvider(
        forced_tool_name=DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    )
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider,
        NativeFakeToolExecutor(),
        nutrition_recommendation_enabled=False,
    )

    with pytest.raises(AgentExecutionError) as caught:
        asyncio.run(
            orchestrator.invoke(
                "multiturn_chat",
                _recommendation_request().model_dump(mode="json"),
            )
        )

    assert caught.value.error_type == "disabled_agent_delegation"
    assert caught.value.retryable is False
