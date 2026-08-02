from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_app.errors import AgentExecutionError
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from shared.schemas import ToolCallResult
from shared.tool_names import (
    PROPOSE_NOTIFICATION_POLICY,
)
from tests.support.agent_scenarios import (
    NativeFakeToolExecutor,
    build_daily_pattern,
    build_missed_payload,
)
from tests.support.llm import NativeChatProvider


class EventToolFinalizingProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []
        self.finalized_tool_names: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        response_mode = user_payload.get("response_mode")
        if response_mode == "daily_pattern_analysis":
            return {
                "summary": "아침 복약 누락 패턴을 확인했습니다.",
                "tool_call": {
                    "name": PROPOSE_NOTIFICATION_POLICY,
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "아침 복약 누락 반복",
                        "source": "pattern_analysis",
                    },
                },
            }
        if response_mode == "missed_dose_message_generation":
            return {
                "generated_message": (
                    "오늘 복약이 어려우셨나요? 현재 상태를 알려주세요."
                )
            }
        raise AssertionError(f"unexpected_response_mode:{response_mode}")

    async def finalize_tool_results(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tool_results: list[ToolCallResult],
    ) -> dict[str, Any]:
        self.finalized_tool_names.append([result.tool_name for result in tool_results])
        if user_payload.get("response_mode") == "daily_pattern_analysis":
            return {"summary": "알림 정책 변경 후보를 준비했습니다."}
        return {
            "patient_message": "메스꺼움 상태를 조금 더 확인할게요.",
            "likely_reason": "side_effect",
            "side_effect_signal": True,
            "symptom_summary": "메스꺼움",
            "follow_up_questions": ["지금도 메스꺼움이 있나요?"],
            "recommendation": "증상 문항에 답해주세요.",
            "missed_dose_hybrid": {
                "reason": "possible_side_effect",
                "generated_message": "메스꺼움 상태를 조금 더 확인할게요.",
                "tone_key": "",
                "safety_notes": ["no_medication_name", "no_diagnosis", "non_directive"],
            },
        }


class NoToolEventProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "generated_message": (
                "오늘 복약이 어려우셨나요? 현재 상태를 알려주세요."
            ),
        }


class HybridOnlyMissedDoseProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "generated_message": (
                "\uc810\uc2ec \uc2dc\uac04\uc5d0 \ub193\uce58\uc2e0 \uac83 \uac19\uc544\uc694. "
                "\uc5b4\ub5a4 \uc5b4\ub824\uc6c0\uc774 \uc788\uc5c8\ub098\uc694?"
            )
        }


class BlankDailyPatternFinalizingProvider(NativeChatProvider):
    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "summary": "아침 복약 누락 패턴을 확인했습니다.",
            "tool_call": {
                "name": PROPOSE_NOTIFICATION_POLICY,
                "arguments": {
                    "slot_label": "아침 08:00",
                    "extra_reminders": 2,
                    "interval_minutes": 10,
                    "effective_start_date": "2026-04-20",
                    "effective_end_date": "2026-04-27",
                    "reason": "아침 복약 누락 반복",
                    "source": "pattern_analysis",
                },
            },
        }

    async def finalize_tool_results(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tool_results: list[ToolCallResult],
    ) -> dict[str, Any]:
        return {"summary": "   "}


def test_daily_pattern_state_graph_uses_one_tool_round_and_unbound_finalizer():
    provider = EventToolFinalizingProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, NativeFakeToolExecutor())

    response = asyncio.run(orchestrator.invoke("daily_pattern", build_daily_pattern().model_dump(mode="json")))

    assert provider.chat_model_bound_tool_history == [[PROPOSE_NOTIFICATION_POLICY], []]
    assert provider.finalized_tool_names == [[PROPOSE_NOTIFICATION_POLICY]]
    assert response.structured_payload["agent_graph_mode"] == "langgraph_state_graph"
    assert response.structured_payload["tool_execution_mode"] == "single_round"
    assert response.structured_payload["finalization_mode"] == "llm_after_tool_result"
    assert response.structured_payload["message_flow"] == [
        "HumanMessage",
        "AIMessage(tool_calls)",
        "ToolMessage",
        "AIMessage(final_answer)",
    ]


def test_daily_pattern_without_final_llm_answer_fails_closed():
    orchestrator = AgentLangGraphNativeOrchestrator(
        BlankDailyPatternFinalizingProvider(),
        NativeFakeToolExecutor(),
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            orchestrator.invoke(
                "daily_pattern",
                build_daily_pattern().model_dump(mode="json"),
            )
        )

    assert exc_info.value.error_type == "llm_final_answer_missing"


def test_missed_dose_uses_one_tool_free_llm_generation():
    provider = EventToolFinalizingProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    assert provider.chat_model_bound_tool_history == [[]]
    assert provider.finalized_tool_names == []
    assert executor.calls == []
    assert response.structured_payload["missed_dose_hybrid"]["generated_message"]
    assert (
        response.structured_payload["agent_graph_mode"]
        == "llm_policy_bounded_generation"
    )
    assert response.structured_payload["tool_execution_mode"] == "none"
    assert response.structured_payload["iterations"] == 1
    assert response.structured_payload["tools_executed"] is False
    assert (
        response.structured_payload["final_answer_source"]
        == "llm_structured_output"
    )


def test_missed_dose_uses_llm_message_instead_of_predefined_policy_message():
    provider = HybridOnlyMissedDoseProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, NativeFakeToolExecutor())

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    expected = "점심 시간에 놓치신 것 같아요. 어떤 어려움이 있었나요?"
    assert response.human_summary == expected
    assert response.structured_payload["missed_dose_hybrid"]["generated_message"] == expected
    assert response.human_summary != (
        build_missed_payload().tone_policy_context["message"]
    )
    assert response.structured_payload["final_answer_source"] == "llm_structured_output"
    assert provider.chat_model_bound_tool_history == [[]]


def test_reused_event_graph_keeps_concurrent_invocation_state_isolated():
    provider = NoToolEventProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, NativeFakeToolExecutor())
    first = build_missed_payload().model_copy(update={"dose_event_id": 101})
    second = build_missed_payload().model_copy(update={"dose_event_id": 202})
    agent_id = id(orchestrator.missed_dose_agent)

    async def invoke_both():
        return await asyncio.gather(
            orchestrator.invoke("missed_dose", first.model_dump(mode="json")),
            orchestrator.invoke("missed_dose", second.model_dump(mode="json")),
        )

    first_response, second_response = asyncio.run(invoke_both())

    assert id(orchestrator.missed_dose_agent) == agent_id
    assert first_response.trace_id != second_response.trace_id
    assert first_response.structured_payload["dose_event_id"] == 101
    assert second_response.structured_payload["dose_event_id"] == 202
    assert provider.chat_model_bound_tool_history == [[], []]
