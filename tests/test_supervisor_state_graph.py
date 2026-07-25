from __future__ import annotations

import asyncio
from datetime import datetime

from agent_app.agents.tool_chat import AGENT_TOOL_LOOP_LIMIT
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.tools.names import (
    DELEGATE_TO_MEDICATION_AGENT,
    DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
    DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    GET_MEDICATION_DOSE_STATUS,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.schemas import MultiturnChatRequest
from tests.support.llm import NativeChatProvider
from tests.test_agent_app_langgraph_native import (
    NativeDelegatingMedicationProvider,
    NativeFakeToolExecutor,
    NativeRecentChatProvider,
    build_taken_chat_request,
)


def test_supervisor_state_graph_direct_answer_uses_one_bound_model_call():
    provider = NativeRecentChatProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, NativeFakeToolExecutor())
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="안녕하세요.",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={},
    )
    graph_id = id(orchestrator.multiturn_chat_agent.graph)

    response = asyncio.run(orchestrator.invoke("multiturn_chat", request.model_dump(mode="json")))

    assert id(orchestrator.multiturn_chat_agent.graph) == graph_id
    assert len(provider.chat_model_bound_tool_history) == 1
    assert provider.chat_model_bound_tool_history[0]
    assert response.structured_payload["routing_mode"] == "direct_answer"
    assert response.structured_payload["agent_graph_mode"] == "langgraph_state_graph"
    assert response.structured_payload["tool_execution_mode"] == "iterative"
    assert "finalization_mode" not in response.structured_payload


def test_supervisor_delegation_returns_to_bound_llm_before_final_response():
    provider = NativeDelegatingMedicationProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)

    response = asyncio.run(orchestrator.invoke("multiturn_chat", build_taken_chat_request().model_dump(mode="json")))

    supervisor_decision_calls = [tools for tools in provider.chat_model_bound_tool_history if DELEGATE_TO_MEDICATION_AGENT in tools]
    assert len(supervisor_decision_calls) == 2
    assert DELEGATE_TO_MEDICATION_AGENT in provider.chat_model_bound_tool_history[-1]
    assert [call["name"] for call in executor.calls] == [UPDATE_MEDICATION_DOSE_EVENT_STATUS]
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["agent_graph_mode"] == "langgraph_state_graph"
    assert response.structured_payload["tool_execution_mode"] == "iterative"
    assert response.structured_payload["finalization_mode"] == "llm_without_tool_calls"
    assert response.structured_payload["tool_loop_mode"] == "langgraph_state_graph"
    assert response.structured_payload["iterations"] == 1
    assert response.structured_payload["message_flow"] == [
        "HumanMessage",
        "AIMessage(tool_calls)",
        "ToolMessage",
        "AIMessage(final_answer)",
    ]


class SequentialNutritionSupervisorProvider(NativeChatProvider):
    async def model_output(self, system_prompt, user_payload):
        response_mode = user_payload.get("response_mode")
        if response_mode == "multiturn_chat":
            return {
                "message": "I will save the allergy before requesting a dinner recommendation.",
                "tool_call": {
                    "name": DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
                    "arguments": {
                        "task": "Save the explicitly stated apple allergy.",
                        "reason": "The recommendation must use the newly stated allergy.",
                    },
                },
            }
        if response_mode == "nutrition_management_chat":
            return {
                "message": "I will save the apple allergy.",
                "tool_call": {
                    "name": UPSERT_NUTRITION_PREFERENCE_FACT,
                    "arguments": {
                        "predicate": "allergic_to",
                        "object_label": "apple",
                    },
                },
            }
        if response_mode == "nutrition_recommendation_chat":
            return {
                "message": "I will find dinner candidates that exclude apples.",
                "tool_call": {
                    "name": GET_NUTRITION_RECOMMENDATION_CANDIDATES,
                    "arguments": {
                        "constraints": {"allergens": ["apple"]},
                        "meal_type": "dinner",
                    },
                },
            }
        raise AssertionError(f"unexpected_response_mode:{response_mode}")

    async def finalize_tool_results(self, system_prompt, user_payload, tool_results):
        response_mode = user_payload.get("response_mode")
        last_tool_name = tool_results[-1].tool_name
        if response_mode == "nutrition_management_chat":
            return {"message": "The apple allergy was saved."}
        if response_mode == "nutrition_recommendation_chat":
            return {"message": "Dinner candidates excluding apples are ready."}
        if response_mode == "multiturn_chat" and last_tool_name == DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT:
            return {
                "message": "The allergy is saved, so I will now request dinner recommendations.",
                "tool_call": {
                    "name": DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
                    "arguments": {
                        "task": "Recommend dinner candidates excluding the saved apple allergy.",
                        "reason": "Preference management is complete and recommendation work remains.",
                    },
                },
            }
        if response_mode == "multiturn_chat" and last_tool_name == DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT:
            return {"message": "I saved your apple allergy and prepared suitable dinner candidates below."}
        raise AssertionError(f"unexpected_finalization:{response_mode}:{last_tool_name}")


def test_supervisor_loops_across_preference_management_and_recommendation():
    provider = SequentialNutritionSupervisorProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="What should I eat for dinner? I am allergic to apples.",
        current_time=datetime(2026, 4, 20, 18, 0),
        context={},
    )

    response = asyncio.run(orchestrator.invoke("multiturn_chat", request.model_dump(mode="json")))

    structured = response.structured_payload
    assert response.agent_name == "multiturn_chat_agent"
    assert [call["name"] for call in structured["supervisor_tool_calls"]] == [
        DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
        DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    ]
    assert structured["delegated_agents"] == [
        "nutrition_management_agent",
        "nutrition_recommendation_agent",
    ]
    assert [call["name"] for call in structured["specialist_tool_calls"]] == [
        UPSERT_NUTRITION_PREFERENCE_FACT,
        GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    ]
    assert [call["name"] for call in executor.calls] == [
        UPSERT_NUTRITION_PREFERENCE_FACT,
        GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    ]
    assert structured["iterations"] == 2
    assert structured["tool_execution_mode"] == "iterative"
    assert "apple allergy" in response.human_summary


class BatchedNutritionSupervisorProvider(SequentialNutritionSupervisorProvider):
    async def model_output(self, system_prompt, user_payload):
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "I will save the allergy and request a suitable dinner recommendation.",
                "tool_calls": [
                    {
                        "name": DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
                        "arguments": {
                            "task": "Save the explicitly stated apple allergy.",
                            "reason": "The recommendation must use the newly stated allergy.",
                        },
                    },
                    {
                        "name": DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
                        "arguments": {
                            "task": "Recommend dinner candidates excluding the apple allergy.",
                            "reason": "The user requested a dinner recommendation.",
                        },
                    },
                ],
            }
        return await super().model_output(system_prompt, user_payload)


def test_supervisor_executes_every_delegation_from_one_model_response():
    provider = BatchedNutritionSupervisorProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="What should I eat for dinner? I am allergic to apples.",
        current_time=datetime(2026, 4, 20, 18, 0),
        context={},
    )

    response = asyncio.run(orchestrator.invoke("multiturn_chat", request.model_dump(mode="json")))

    structured = response.structured_payload
    assert [call["name"] for call in structured["supervisor_tool_calls"]] == [
        DELEGATE_TO_NUTRITION_MANAGEMENT_AGENT,
        DELEGATE_TO_NUTRITION_RECOMMENDATION_AGENT,
    ]
    assert structured["delegated_agents"] == [
        "nutrition_management_agent",
        "nutrition_recommendation_agent",
    ]
    assert [call["name"] for call in executor.calls] == [
        UPSERT_NUTRITION_PREFERENCE_FACT,
        GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    ]
    assert structured["iterations"] == 1


class RepeatingMedicationDelegationProvider(NativeDelegatingMedicationProvider):
    async def finalize_tool_results(self, system_prompt, user_payload, tool_results):
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "I will ask the medication specialist again.",
                "tool_call": {
                    "name": DELEGATE_TO_MEDICATION_AGENT,
                    "arguments": {
                        "task": "Check the medication request again.",
                        "reason": "repeat_for_loop_limit_test",
                    },
                },
            }
        if user_payload.get("response_mode") == "medication_chat":
            return {"message": "The medication update was completed."}
        raise AssertionError(f"unexpected_response_mode:{user_payload.get('response_mode')}")


def test_supervisor_returns_technical_failure_at_tool_loop_limit():
    provider = RepeatingMedicationDelegationProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)

    response = asyncio.run(orchestrator.invoke("multiturn_chat", build_taken_chat_request().model_dump(mode="json")))

    structured = response.structured_payload
    assert response.decision_type == "tool_loop_limit_exceeded"
    assert structured["routing_mode"] == "supervisor_max_iterations"
    assert structured["iterations"] == AGENT_TOOL_LOOP_LIMIT
    assert len(structured["supervisor_tool_calls"]) == AGENT_TOOL_LOOP_LIMIT
    assert len(structured["pending_tool_calls"]) == 1
    assert len(executor.calls) == AGENT_TOOL_LOOP_LIMIT


class MedicationStatusSummaryProvider(NativeDelegatingMedicationProvider):
    async def model_output(self, system_prompt, user_payload):
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        response_mode = user_payload.get("response_mode")
        if response_mode == "multiturn_chat":
            return {
                "message": "\ubcf5\uc57d \uc870\ud68c\ub97c \ub2f4\ub2f9 \uc5d0\uc774\uc804\ud2b8\uc5d0\uac8c \uc694\uccad\ud558\uaca0\uc2b5\ub2c8\ub2e4.",
                "tool_call": {
                    "name": DELEGATE_TO_MEDICATION_AGENT,
                    "arguments": {
                        "task": "\uc9c0\uae08\uae4c\uc9c0\uc758 \uc57d \ubcf5\uc6a9 \uc0c1\ud669\uc744 \uc870\ud68c\ud558\uace0 \uc694\uc57d\ud574 \uc8fc\uc138\uc694.",
                        "reason": "\uc0ac\uc6a9\uc790\uac00 \ubcf5\uc57d \ud604\ud669 \uc870\ud68c\ub97c \uc694\uccad\ud588\uc2b5\ub2c8\ub2e4.",
                    },
                },
            }
        if response_mode == "medication_chat":
            return {
                "message": "\ubcf5\uc57d \uae30\ub85d\uc744 \uc870\ud68c\ud558\uaca0\uc2b5\ub2c8\ub2e4.",
                "tool_call": {
                    "name": GET_MEDICATION_DOSE_STATUS,
                    "arguments": {"target_date": "2026-04-20"},
                },
            }
        return {"message": "\uc694\uccad\uc744 \ud655\uc778\ud588\uc2b5\ub2c8\ub2e4."}

    async def finalize_tool_results(self, system_prompt, user_payload, tool_results):
        return {
            "message": "\uc624\ub298 \ubcf5\uc57d \ud604\ud669\uc740 \uc544\uce68 08:00 \uc57d 1\ud68c \ubcf5\uc6a9 \uc644\ub8cc\uc785\ub2c8\ub2e4.",
            "advice": "\ub2e4\uc74c \ubcf5\uc57d \uc77c\uc815\ub3c4 \uc78a\uc9c0 \ub9d0\uace0 \ucc59\uaca8\uc8fc\uc138\uc694.",
        }


def test_supervisor_preserves_medication_status_message_when_finalizer_returns_advice():
    provider = MedicationStatusSummaryProvider()
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider, executor)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="\uc9c0\uae08\uae4c\uc9c0\uc758 \uc57d \ubcf5\uc6a9\uc0c1\ud669\uc744 \uc54c\uace0 \uc2f6\uc5b4",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={},
    )

    response = asyncio.run(orchestrator.invoke("multiturn_chat", request.model_dump(mode="json")))

    assert [call["name"] for call in executor.calls] == [GET_MEDICATION_DOSE_STATUS]
    assert "\uc624\ub298 \ubcf5\uc57d \ud604\ud669" in response.human_summary
    assert "\uc544\uce68 08:00" in response.human_summary
    assert "\ub2e4\uc74c \ubcf5\uc57d \uc77c\uc815" in response.human_summary
