from __future__ import annotations

import asyncio
from typing import Any

from agent_app.graph import AgentLangGraphNativeOrchestrator
from agent_app.tool_names import (
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    PROPOSE_NOTIFICATION_POLICY,
)
from shared.schemas import ToolCallResult
from tests.test_agent_app_langgraph_native import (
    NativeChatProvider,
    NativeFakeToolExecutor,
    build_daily_pattern,
    build_missed_payload,
)


class EventToolFinalizingProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []
        self.finalized_tool_names: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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
        if response_mode == "missed_dose_coaching":
            return {
                "patient_message": "증상을 확인해볼게요.",
                "tool_call": {
                    "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                    "arguments": {"symptom_text": "메스꺼움"},
                },
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

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "patient_message": "현재 복용 가능한 상태인지 알려주세요.",
            "likely_reason": "unknown",
            "follow_up_questions": ["현재 복용 가능한가요?"],
        }


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


def test_missed_dose_state_graph_finalizer_preserves_required_patient_payload():
    provider = EventToolFinalizingProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    assert set(provider.chat_model_bound_tool_history[0]) == {
        GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        GET_PRO_CTCAE_QUESTIONNAIRE,
    }
    assert provider.chat_model_bound_tool_history[1] == []
    assert provider.finalized_tool_names == [[GET_MEDICATION_SIDE_EFFECT_ASSESSMENT]]
    assert len(executor.calls) == 1
    assert response.structured_payload["side_effect_signal"] is True
    assert response.structured_payload["missed_dose_hybrid"]["generated_message"]
    assert response.structured_payload["finalization_mode"] == "llm_after_tool_result"


def test_reused_event_graph_keeps_concurrent_invocation_state_isolated():
    provider = NoToolEventProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, NativeFakeToolExecutor())
    first = build_missed_payload().model_copy(update={"dose_event_id": 101})
    second = build_missed_payload().model_copy(update={"dose_event_id": 202})
    graph_id = id(orchestrator.missed_dose_agent.graph_runner.graph)

    async def invoke_both():
        return await asyncio.gather(
            orchestrator.invoke("missed_dose", first.model_dump(mode="json")),
            orchestrator.invoke("missed_dose", second.model_dump(mode="json")),
        )

    first_response, second_response = asyncio.run(invoke_both())

    assert id(orchestrator.missed_dose_agent.graph_runner.graph) == graph_id
    assert first_response.trace_id != second_response.trace_id
    assert first_response.structured_payload["dose_event_id"] == 101
    assert second_response.structured_payload["dose_event_id"] == 202
    assert len(provider.chat_model_bound_tool_history) == 2
    assert all(history for history in provider.chat_model_bound_tool_history)
