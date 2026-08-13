from __future__ import annotations

import asyncio
from typing import Any

from agent_app.agents.medication import MedicationAgent
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import ToolCallResult
from shared.tool_names import (
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    MEDICATION_AGENT_CALLABLE_TOOLS,
    MEDICATION_CHAT_TOOLS,
)
from tests.support.llm import NativeChatProvider


class NegativeSideEffectProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []
        self.final_payload: dict[str, Any] | None = None

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        raise AssertionError(
            "clinical continuation must skip the planning model turn"
        )

    async def finalize_tool_results(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tool_results,
    ) -> dict[str, Any]:
        self.final_payload = user_payload
        return {
            "message": (
                "The available medication information did not identify "
                "the symptom as a suspected medication-related effect."
            )
        }


class NegativeSideEffectExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool_call(
        self,
        tool_call,
        *,
        trace_id,
        source_event_type,
        payload,
    ) -> ToolCallResult:
        self.calls.append(tool_call)
        return ToolCallResult(
            tool_name=tool_call["name"],
            status="success",
            response={
                "suspected": False,
                "matched_items": [],
                "matched_effects": [],
                "severity": "none",
                "assessments": [],
            },
        )


def test_medication_callable_tools_exclude_server_owned_questionnaire():
    assert GET_PRO_CTCAE_QUESTIONNAIRE in MEDICATION_CHAT_TOOLS
    assert GET_PRO_CTCAE_QUESTIONNAIRE not in MEDICATION_AGENT_CALLABLE_TOOLS
    assert (
        GET_MEDICATION_SIDE_EFFECT_ASSESSMENT
        in MEDICATION_AGENT_CALLABLE_TOOLS
    )


def test_negative_assessment_does_not_prepare_questionnaire():
    provider = NegativeSideEffectProvider()
    executor = NegativeSideEffectExecutor()
    agent = MedicationAgent(provider, ToolRuntime(executor))

    response = asyncio.run(
        agent.continue_with_tool_calls(
            "trace-negative-side-effect",
            {
                "message": "My voice has changed. Could it be my medication?",
                "context": {},
            },
            tool_calls=[
                {
                    "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                    "arguments": {
                        "symptom_mentions": [
                            {"text": "My voice has changed."}
                        ]
                    },
                }
            ],
        )
    )

    assert response.decision_type == "tool_call"
    assert [call["name"] for call in executor.calls] == [
        GET_MEDICATION_SIDE_EFFECT_ASSESSMENT
    ]
    bound_tools = set(provider.chat_model_bound_tool_history[-1])
    assert GET_MEDICATION_SIDE_EFFECT_ASSESSMENT in bound_tools
    assert GET_PRO_CTCAE_QUESTIONNAIRE not in bound_tools
    assert provider.final_payload is not None
    available_tools = {
        tool["name"] for tool in provider.final_payload["available_tools"]
    }
    assert GET_MEDICATION_SIDE_EFFECT_ASSESSMENT in available_tools
    assert GET_PRO_CTCAE_QUESTIONNAIRE not in available_tools
