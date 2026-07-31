from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_app.agents.medication import MedicationAgent
from agent_app.agents.nutrition_management import NutritionManagementAgent
from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.errors import AgentExecutionError
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
)
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import ToolCallResult
from tests.support.llm import NativeChatProvider


class ConcurrentSpecialistProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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
                "trace-third",
                {"message": "세 번째 요청", "context": {}},
            ),
        )

    first, second, third = asyncio.run(invoke_all())

    assert id(agent.graph_runner.graph) == graph_id
    assert first.trace_id == "trace-first"
    assert second.trace_id == "trace-second"
    assert third.trace_id == "trace-third"
    assert first.human_summary == "전문 응답: 첫 번째 요청"
    assert second.human_summary == "전문 응답: 두 번째 요청"
    assert third.human_summary == "전문 응답: 세 번째 요청"
    assert len(provider.chat_model_bound_tool_history) == 3
    assert all(history for history in provider.chat_model_bound_tool_history)


class ApprovedWriteFinalizationProvider(NativeChatProvider):
    def __init__(self, *, try_second_write: bool = False) -> None:
        self.try_second_write = try_second_write
        self.chat_model_bound_tool_history: list[list[str]] = []

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        output: dict[str, Any] = {"message": "점심 식사 기록을 저장했습니다."}
        if self.try_second_write:
            output["tool_call"] = {
                "name": CREATE_NUTRITION_MEAL_RECORD,
                "arguments": {
                    "meal_type": "lunch",
                    "foods": [{"food_name": "중복 식사"}],
                },
            }
        return output


class SuccessfulWriteExecutor:
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
            response={"success": True, "record_id": "meal-public-1"},
        )


def _confirmed_meal_tool_call() -> list[dict[str, Any]]:
    return [
        {
            "id": "approved-meal-write",
            "name": CREATE_NUTRITION_MEAL_RECORD,
            "arguments": {
                "approval_key": "apv_abcdefghijklmnopqrstuvwx",
            },
        }
    ]


def _confirmed_meal_payload() -> dict[str, Any]:
    tool_call = _confirmed_meal_tool_call()[0]
    return {
        "message": "기록해 주세요.",
        "context": {
            "approved_user_action": {
                "status": "confirmed",
                "action_name": tool_call["name"],
                "arguments": tool_call["arguments"],
            }
        },
    }


def test_approved_write_uses_dedicated_seeded_entry_for_finalization() -> None:
    provider = ApprovedWriteFinalizationProvider()
    executor = SuccessfulWriteExecutor()
    agent = NutritionManagementAgent(provider, ToolRuntime(executor))

    response = asyncio.run(
        agent.execute_approved_write(
            "trace-confirmed-meal",
            _confirmed_meal_payload(),
            tool_call=_confirmed_meal_tool_call()[0],
        )
    )

    assert response.human_summary == "점심 식사 기록을 저장했습니다."
    assert [call["name"] for call in executor.calls] == [
        CREATE_NUTRITION_MEAL_RECORD
    ]
    assert len(provider.chat_model_bound_tool_history) == 1
    assert provider.chat_model_bound_tool_history[0] == []


def test_approved_write_rejects_a_second_model_tool_call() -> None:
    provider = ApprovedWriteFinalizationProvider(try_second_write=True)
    executor = SuccessfulWriteExecutor()
    agent = NutritionManagementAgent(provider, ToolRuntime(executor))

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            agent.execute_approved_write(
                "trace-no-duplicate-meal",
                _confirmed_meal_payload(),
                tool_call=_confirmed_meal_tool_call()[0],
            )
        )

    assert (
        exc_info.value.error_type
        == "approved_write_followup_tool_forbidden"
    )
    assert len(executor.calls) == 1
    assert len(provider.chat_model_bound_tool_history) == 1
    assert provider.chat_model_bound_tool_history[0] == []


class ClinicalContinuationProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.finalization_count = 0
        self.chat_model_bound_tool_history: list[list[str]] = []

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
        self.finalization_count += 1
        if self.finalization_count == 1:
            return {
                "tool_call": {
                    "name": GET_SIDE_EFFECT_HISTORY,
                    "arguments": {"limit": 5},
                }
            }
        return {
            "message": (
                "메스꺼움 관련 설문 문항을 준비했습니다."
            )
        }


class ClinicalContinuationExecutor:
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
        tool_name = tool_call["name"]
        if tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
            response = {
                "suspected": True,
                "matched_effects": ["메스꺼움"],
                "matched_items": ["메트포르민 500mg"],
            }
        elif tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
            response = {
                "matched": True,
                "questions": [
                    {
                        "question_id": "frequency",
                        "question": "얼마나 자주 느꼈습니까?",
                        "options": ["전혀 없다", "자주 있다"],
                    }
                ],
            }
        else:
            response = {"items": []}
        return ToolCallResult(
            tool_name=tool_name,
            status="success",
            response=response,
        )


def test_clinical_continuation_allows_agentic_followup_tool_call() -> None:
    provider = ClinicalContinuationProvider()
    executor = ClinicalContinuationExecutor()
    agent = MedicationAgent(provider, ToolRuntime(executor))

    response = asyncio.run(
        agent.continue_with_tool_calls(
            "trace-clinical-continuation",
            {
                "message": "어제 약 먹고 속이 메스꺼웠어",
                "context": {},
            },
            tool_calls=[
                {
                    "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                    "arguments": {
                        "symptom_text": "메스꺼움",
                        "symptom_onset_text": "어제 약 복용 후",
                    },
                }
            ],
        )
    )

    assert response.human_summary == (
        "메스꺼움 관련 설문 문항을 준비했습니다."
    )
    assert [call["name"] for call in executor.calls] == [
        GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        GET_PRO_CTCAE_QUESTIONNAIRE,
        GET_SIDE_EFFECT_HISTORY,
    ]


def test_confirmation_response_uses_backend_display_question() -> None:
    graph = object.__new__(ToolChatAgentGraph)
    graph.agent_name = "medication_agent"
    response = graph._confirmation_response(
        {
            "trace_id": "trace-confirmation-copy",
            "all_executed_calls": [
                {
                    "name": "request_record_approval",
                    "arguments": {},
                }
            ],
            "all_results": [
                ToolCallResult(
                    tool_name="request_record_approval",
                    status="confirmation_required",
                    response={
                        "mutation_confirmation": {
                            "display": {
                                "question": (
                                    "부작용 평가 기록을 저장할까요?"
                                )
                            }
                        }
                    },
                )
            ],
            "all_tool_messages": [],
            "iterations": 1,
        }
    )["response"]

    assert response.human_summary == (
        "부작용 평가 기록을 저장할까요?"
    )
