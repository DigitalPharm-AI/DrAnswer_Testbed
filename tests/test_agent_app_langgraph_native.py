from __future__ import annotations

import ast
import asyncio
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

import agent_app.main as native_agent_main
from agent_app.agent_delegation import delegation_tools_payload
from agent_app.agents.tool_chat import SPECIALIST_TOOL_LOOP_LIMIT, ToolChatAgentGraph
from agent_app.async_tasks import DEAD, enqueue_async_task
from agent_app.continuation_policy import async_continuation_type
from agent_app.chat_tooling import (
    ai_message_from_tool_calls,
    human_payload_from_messages,
    langchain_tool_name,
    system_prompt_from_messages,
    tool_results_from_messages,
)
from agent_app.errors import AgentExecutionError
from agent_app.graph import AgentLangGraphNativeOrchestrator
from agent_app.output_validation import validate_llm_output
from agent_app.prompt_builders import multiturn_chat_prompt, nutrition_management_agent_prompt, nutrition_recommendation_agent_prompt
from agent_app.providers import BaseLLMProvider, RuleBasedProvider, create_llm_provider
from agent_app.response_builders import missed_dose_hybrid_payload, natural_chat_summary
from agent_app.tool_calling import normalize_tool_calls
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_executor import McpAgentToolExecutor
from agent_app.tool_policy import _notification_policy_deltas, deferred_policy_tool_result, is_deferred_policy_tool_call
from agent_app.tool_permissions import permission_denied_result, validate_tool_permission
from agent_app.tool_mcp_server import http_status_tool_error_result
from agent_app.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_SIDE_EFFECT_HISTORY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    LEGACY_TOOL_NAMES,
    SOURCE_MEDICATION_AGENT,
    SOURCE_MULTITURN_CHAT,
    SOURCE_NUTRITION_MANAGEMENT_AGENT,
    SOURCE_NUTRITION_RECOMMENDATION_AGENT,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    replace_legacy_tool_names,
)
from agent_app.tool_protocol import (
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    mcp_json_rpc_request,
    mcp_result_from_tool_result,
    mcp_tools_list,
    tool_result_from_mcp_result,
)
from agent_app.tool_runtime import ToolRuntime
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_side_effects import ae_tool_call_from_lookup
from shared.schemas import AgentCallbackContext, DailyMedicationPattern, DosePatternEvent, MissedDoseEventPayload, MultiturnChatRequest, SlotAdherenceSummary, ToolCallResult
from shared.settings import get_settings


class NativeChatProvider(BaseLLMProvider):
    def chat_model(self):
        return NativeProviderChatModel(provider=self)


class NativeProviderChatModel(BaseChatModel):
    provider: Any
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "native_test_chat_model"

    def bind_tools(self, tools, *, tool_choice: str | None = None, **kwargs):
        setattr(self.provider, "bound_tool_names", [langchain_tool_name(tool) for tool in tools])
        return self.model_copy(update={"bound_tools": list(tools)})

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, run_manager=None, **kwargs))

    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        history = getattr(self.provider, "chat_model_bound_tool_history", [])
        history.append([langchain_tool_name(tool) for tool in self.bound_tools])
        setattr(self.provider, "chat_model_bound_tool_history", history)

        payload = human_payload_from_messages(messages)
        tool_results = tool_results_from_messages(messages)
        if tool_results:
            finalizer = getattr(self.provider, "finalize_tool_results", None)
            if callable(finalizer):
                output = await finalizer(system_prompt_from_messages(messages), payload, tool_results)
                if not isinstance(output, dict):
                    output = {}
                return _chat_result(AIMessage(content=natural_chat_summary(output), response_metadata={"model_output": output}))
            fallback = tool_result_summary(tool_results, "도구 실행 결과를 확인했습니다.")
            return _chat_result(AIMessage(content=fallback, response_metadata={"model_output": {"message": fallback, "fallback": "tool_result_summary"}}))

        output = await self.provider.generate_json(system_prompt_from_messages(messages), payload)
        if not isinstance(output, dict):
            output = {}
        tool_calls = normalize_tool_calls(output)
        if tool_calls:
            return _chat_result(ai_message_from_tool_calls(tool_calls, content=natural_chat_summary(output), model_output=output))
        return _chat_result(AIMessage(content=natural_chat_summary(output), response_metadata={"model_output": output}))


def _chat_result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


class NativeFakeProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.seen_prompts: list[str] = []
        self.bound_tool_names: list[str] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_prompts.append(system_prompt)
        self.seen_payloads.append(user_payload)
        response_mode = user_payload.get("response_mode")
        if response_mode == "daily_pattern_analysis":
            return {
                "summary": "아침 시간대 미복용이 반복됩니다.",
                "tool_call": {
                    "name": "propose_notification_policy",
                    "arguments": {
                        "slot_label": "아침 08:00",
                        "extra_reminders": 2,
                        "interval_minutes": 10,
                        "effective_start_date": "2026-04-20",
                        "effective_end_date": "2026-04-27",
                        "reason": "아침 시간대 미복용이 반복됩니다.",
                        "source": "pattern_analysis",
                    },
                },
            }
        if response_mode == "missed_dose_coaching":
            return {
                "patient_message": "아침 08:00 혈압약 복약을 놓치신 것으로 확인됐어요. 현재 복용 가능하신가요?",
                "likely_reason": "unknown",
                "side_effect_signal": False,
                "symptom_summary": "",
                "follow_up_questions": ["현재 복용 가능하신가요?"],
                "recommendation": "현재 상태와 미복용 이유를 확인하세요.",
            }
        if "복용" in str(user_payload.get("message", "")):
            return {
                "message": "복약 완료를 기록하겠습니다.",
                "tool_call": {
                    "name": "update_medication_dose_event_status",
                    "arguments": {
                        "dose_event_id": 12,
                        "reason": "patient_reported_taken",
                    },
                },
            }
        return {"advice": "현재 복약 상태를 확인했습니다.", "observations": ["추가 도구 실행은 필요하지 않습니다."]}


class NativeDelegatingMedicationProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "복약 담당 에이전트가 확인하겠습니다.",
                "tool_call": {
                    "name": "delegate_to_medication_agent",
                    "arguments": {
                        "task": "record reported dose as taken",
                        "reason": "patient reported taking a current medication dose",
                    },
                },
            }
        if user_payload.get("response_mode") == "medication_chat":
            return {
                "message": "복약 완료를 기록하겠습니다.",
                "tool_call": {
                    "name": "update_medication_dose_event_status",
                    "arguments": {
                        "dose_event_id": 12,
                        "reason": "patient_reported_taken",
                    },
                },
            }
        return {"advice": "확인했습니다."}


class NativeDelegatingNutritionManagementProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "영양 관리 에이전트가 확인하겠습니다.",
                "tool_call": {
                    "name": "delegate_to_nutrition_management_agent",
                    "arguments": {
                        "task": "search food candidates before saving the meal",
                        "reason": "patient reported eating a food item",
                    },
                },
            }
        if user_payload.get("response_mode") == "nutrition_management_chat":
            return {
                "message": "음식 후보를 확인하겠습니다.",
                "tool_call": {
                    "name": "search_nutrition_food_candidates",
                    "arguments": {"query": "삶은 계란"},
                },
            }
        return {"advice": "확인했습니다."}


class NativeDelegatingNutritionRecommendationProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "영양 추천 에이전트가 확인하겠습니다.",
                "tool_call": {
                    "name": "delegate_to_nutrition_recommendation_agent",
                    "arguments": {
                        "task": "recommend a suitable dinner",
                        "reason": "patient asked what to eat",
                    },
                },
            }
        if user_payload.get("response_mode") == "nutrition_recommendation_chat":
            return {
                "message": "오늘 섭취와 선호도를 반영해 추천하겠습니다.",
                "tool_call": {
                    "name": "get_nutrition_recommendation_candidates",
                    "arguments": {"constraints": {"sodium": "low"}, "meal_type": "dinner"},
                },
            }
        return {"advice": "확인했습니다."}


class NativeMultiStepNutritionFoodUpdateProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    def chat_model(self):
        return NativeMultiStepNutritionFoodUpdateChatModel(provider=self)

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("NativeMultiStepNutritionFoodUpdateChatModel handles generation directly")


class NativeMultiStepNutritionFoodUpdateChatModel(NativeProviderChatModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        payload = human_payload_from_messages(messages)
        tool_results = tool_results_from_messages(messages)
        tool_result_names = [result.tool_name for result in tool_results]
        self.provider.bound_tool_history.append([langchain_tool_name(tool) for tool in self.bound_tools])

        if payload.get("response_mode") == "multiturn_chat" and not tool_result_names:
            return _chat_result(
                ai_message_from_tool_calls(
                    [
                        {
                            "name": "delegate_to_nutrition_management_agent",
                            "arguments": {
                                "task": "change the recorded lunch food from 탕수육 to 꿔바로우",
                                "reason": "patient corrected a previously recorded food item",
                            },
                        }
                    ],
                    content="영양 관리 에이전트가 식사 기록을 확인하겠습니다.",
                    model_output={"message": "영양 관리 에이전트가 식사 기록을 확인하겠습니다."},
                )
            )

        if payload.get("response_mode") == "nutrition_management_chat":
            if not tool_result_names:
                return _chat_result(
                    ai_message_from_tool_calls(
                        [{"name": "get_nutrition_meal_record_list", "arguments": {"meal_date": "2026-04-20"}}],
                        content="수정할 음식의 meal_id와 food_id를 확인하겠습니다.",
                        model_output={"message": "수정할 음식의 meal_id와 food_id를 확인하겠습니다."},
                    )
                )
            if tool_result_names[-1] == "get_nutrition_meal_record_list":
                return _chat_result(
                    ai_message_from_tool_calls(
                        [{"name": "search_nutrition_food_candidates", "arguments": {"query": "꿔바로우"}}],
                        content="새 음식의 영양 정보를 확인하겠습니다.",
                        model_output={"message": "새 음식의 영양 정보를 확인하겠습니다."},
                    )
                )
            if tool_result_names[-1] == "search_nutrition_food_candidates":
                return _chat_result(
                    ai_message_from_tool_calls(
                        [
                            {
                                "name": "update_nutrition_food_record",
                                "arguments": {
                                    "meal_id": 101,
                                    "food_id": 202,
                                    "food_ref_id": "guobaorou",
                                    "food_name": "꿔바로우",
                                    "portion": {"amount": 1, "unit": "serving"},
                                    "nutrients": {"calories": 360, "protein": 18},
                                    "reason": "patient_corrected_food",
                                },
                            }
                        ],
                        content="확인한 food_id로 음식 기록을 수정하겠습니다.",
                        model_output={"message": "확인한 food_id로 음식 기록을 수정하겠습니다."},
                    )
                )
            if tool_result_names[-1] == "update_nutrition_food_record":
                summary = "전문 에이전트가 점심의 탕수육을 꿔바로우로 수정했습니다."
                return _chat_result(AIMessage(content=summary, response_metadata={"model_output": {"message": summary}}))

        if payload.get("response_mode") == "multiturn_chat" and tool_result_names[-1:] == ["delegate_to_nutrition_management_agent"]:
            summary = "점심 식사 기록에서 탕수육을 꿔바로우로 수정했어요."
            return _chat_result(AIMessage(content=summary, response_metadata={"model_output": {"message": summary}}))

        summary = "확인했습니다."
        return _chat_result(AIMessage(content=summary, response_metadata={"model_output": {"message": summary}}))


class NativeSideEffectLookupProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_payloads.append(user_payload)
        self.bound_tool_history.append(list(self.bound_tool_names))
        if user_payload.get("response_mode") == "multiturn_chat":
            return {
                "message": "The medication specialist will assess the symptom.",
                "tool_call": {
                    "name": "delegate_to_medication_agent",
                    "arguments": {
                        "task": "assess a possible medication side effect and prepare PRO-CTCAE questions when indicated",
                        "reason": "patient reported a possible medication-related symptom",
                    },
                },
            }
        if user_payload.get("response_mode") == "medication_chat":
            return {
                "message": "Checking the medication side-effect information first.",
                "tool_call": {
                    "name": "get_medication_side_effect_assessment",
                    "arguments": {
                        "symptom_text": "nausea",
                        "medication_name": "anticancer drug",
                    },
                },
            }
        return {"advice": "The symptom assessment is complete."}

class NativeLoopLimitProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.bound_tool_names: list[str] = []
        self.chat_model_call_count = 0

    def chat_model(self):
        self.chat_model_call_count += 1
        return NativeLoopLimitChatModel(provider=self)

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("NativeLoopLimitChatModel handles generation directly")


class NativeLoopLimitChatModel(NativeProviderChatModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        tool_call = {
            "name": "search_nutrition_food_candidates",
            "arguments": {"query": "계란"},
        }
        return _chat_result(
            ai_message_from_tool_calls(
                [tool_call],
                content="음식 정보를 계속 확인합니다.",
                model_output={"message": "음식 정보를 계속 확인합니다.", "tool_call": tool_call},
            )
        )


class NativeRecentChatProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.seen_prompts: list[str] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_prompts.append(system_prompt)
        self.seen_payloads.append(user_payload)
        return {
            "advice": "앞서 속이 메스꺼운데 약 때문일지 물어보셨고, PRO-CTCAE 문항에는 1번 자주 있다, 2번 보통이다로 답하셨습니다.",
            "observations": ["recent_chat을 참고해 일반 대화로 답변했습니다."],
            "tool_calls": [],
        }


class InvalidMissedDoseHybridProvider(NativeChatProvider):
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert user_payload["response_mode"] == "missed_dose_coaching"
        return {
            "patient_message": "확인했습니다.",
            "likely_reason": "unknown",
        }


class UnsafeMissedDoseToolProvider(NativeChatProvider):
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert user_payload["response_mode"] == "missed_dose_coaching"
        return {
            "patient_message": "복용 완료를 기록하겠습니다.",
            "tool_call": {
                "name": "update_medication_dose_event_status",
                "arguments": {"dose_event_id": 12},
            },
        }


class NativeFakeToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool_call(self, tool_call: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> ToolCallResult:
        name = str(tool_call.get("name"))
        denial_reason = validate_tool_permission(tool_call, source_event_type=source_event_type, payload=payload)
        if denial_reason:
            return permission_denied_result(tool_call, trace_id=trace_id, source_event_type=source_event_type, reason=denial_reason)
        if is_deferred_policy_tool_call(tool_call):
            return deferred_policy_tool_result(tool_call, trace_id=trace_id, source_event_type=source_event_type)
        self.calls.append({**tool_call, "_source_event_type": source_event_type})
        if name == "update_medication_dose_event_status":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "dose_event_id": tool_call["arguments"]["dose_event_id"],
                    "status": "taken",
                    "message": "아침 08:00 혈압약 복약을 완료로 기록했습니다.",
                },
                idempotency_key=f"{trace_id}:update_medication_dose_event_status:{source_event_type}:12",
            )
        if name == GET_MEDICATION_DOSE_STATUS:
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "dose_events": [
                        {
                            "slot_label": "\uc544\uce68 08:00",
                            "status": "taken",
                            "taken_at": "2026-04-20T09:30:00",
                        }
                    ],
                    "total": 1,
                    "totals_by_status": {"taken": 1, "scheduled": 0, "missed": 0},
                },
                idempotency_key=f"{trace_id}:get_medication_dose_status",
            )

        if name == "propose_notification_policy":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "idempotency_key": f"{trace_id}:propose_notification_policy:{source_event_type}",
                    "results": [{"slot_label": "아침 08:00", "applied": True, "message": "정책을 적용했습니다."}],
                    "all_applied": True,
                },
                idempotency_key=f"{trace_id}:propose_notification_policy:{source_event_type}",
            )
        if name == "get_medication_side_effect_assessment":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "suspected": True,
                    "matched_effects": ["메스꺼움"],
                    "matched_items": ["항암제"],
                    "severity": "moderate",
                    "evidence": "주의사항에 메스꺼움이 포함되어 있습니다.",
                    "recommendation": "증상 문항 확인이 필요합니다.",
                },
                idempotency_key=f"{trace_id}:get_medication_side_effect_assessment",
            )
        if name == "get_pro_ctcae_questionnaire":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "input_symptom": tool_call["arguments"]["symptom_normalize"],
                    "matched": True,
                    "match_type": "exact",
                    "matched_symptom_term": "Nausea",
                    "matched_korean_symptom_name": "메스꺼움",
                    "similarity": 1.0,
                    "threshold": 0.56,
                    "scoring_method": "test",
                    "embedding_provider": "",
                    "sheet_name": "Parsed_Items",
                    "questions": [],
                    "candidates": [],
                },
                idempotency_key=f"{trace_id}:get_pro_ctcae_questionnaire",
            )
        if name == "search_nutrition_food_candidates":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "query": tool_call["arguments"].get("query", ""),
                    "candidates": [
                        {
                            "food_ref_id": "egg-boiled",
                            "food_name": "삶은 달걀",
                            "serving_size": 50,
                            "nutrients": {"calories": 70, "protein": 6},
                        }
                    ],
                },
                idempotency_key=f"{trace_id}:search_nutrition_food_candidates",
            )
        if name == "get_nutrition_meal_record_list":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meals": [
                        {
                            "id": 101,
                            "meal_type": "lunch",
                            "meal_label": "점심",
                            "foods": [
                                {
                                    "id": 202,
                                    "food_ref_id": "tangsuyuk",
                                    "food_name": "탕수육",
                                    "portion": {"amount": 1, "unit": "serving"},
                                    "nutrients": {"calories": 420, "protein": 16},
                                }
                            ],
                        }
                    ],
                },
                idempotency_key=f"{trace_id}:get_nutrition_meal_record_list",
            )
        if name == "get_nutrition_recommendation_candidates":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "recommendations": [{"food_name": "두부 샐러드", "score": 0.9}],
                    "blocked_count": 0,
                },
                idempotency_key=f"{trace_id}:get_nutrition_recommendation_candidates",
            )
        if name == "update_nutrition_meal_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={"success": True, "meal": {"id": tool_call["arguments"]["meal_id"], "meal_label": "점심"}, "daily_summary": {}},
                idempotency_key=f"{trace_id}:update_nutrition_meal_record:{tool_call['arguments']['meal_id']}",
            )
        if name == "delete_nutrition_meal_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={"success": True, "deleted_meal": {"id": tool_call["arguments"]["meal_id"], "meal_label": "점심"}, "daily_summary": {}},
                idempotency_key=f"{trace_id}:delete_nutrition_meal_record:{tool_call['arguments']['meal_id']}",
            )
        if name == "update_nutrition_food_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meal": {"id": tool_call["arguments"]["meal_id"], "meal_label": "점심"},
                    "food": {"id": tool_call["arguments"]["food_id"], "food_name": tool_call["arguments"].get("food_name", "수정 음식")},
                    "daily_summary": {},
                },
                idempotency_key=f"{trace_id}:update_nutrition_food_record:{tool_call['arguments']['meal_id']}:{tool_call['arguments']['food_id']}",
            )
        if name == "delete_nutrition_food_record":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "success": True,
                    "meal_id": tool_call["arguments"]["meal_id"],
                    "food_id": tool_call["arguments"]["food_id"],
                    "deleted_food": {"id": tool_call["arguments"]["food_id"], "food_name": "삭제 음식"},
                    "daily_summary": {},
                    "meal_deleted": False,
                },
                idempotency_key=f"{trace_id}:delete_nutrition_food_record:{tool_call['arguments']['meal_id']}:{tool_call['arguments']['food_id']}",
            )
        return ToolCallResult(tool_name=name, status="error", error="unexpected_tool")


class RecordingMcpServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def handle_json_rpc(self, request: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(
            {
                "request": request,
                "trace_id": trace_id,
                "source_event_type": source_event_type,
                "payload": payload,
            }
        )
        result = ToolCallResult(
            tool_name=request["params"]["name"],
            status="success",
            response={"ok": True},
            idempotency_key=f"{trace_id}:{request['params']['name']}",
        )
        return {
            "jsonrpc": "2.0",
            "id": request.get("id"),
            "result": mcp_result_from_tool_result(result),
        }


def internal_auth_headers() -> dict[str, str]:
    token = get_settings().internal_api_token
    return {"X-Internal-Api-Token": token} if token else {}


def test_agent_app_has_no_legacy_imports():
    forbidden = {"agent_app_" + "legacy", "agent_app_old_legacy"}
    root = Path("agent_app")
    violations: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top_level = alias.name.split(".")[0]
                    if top_level in forbidden:
                        violations.append(f"{path}:{node.lineno}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level = node.module.split(".")[0]
                if top_level in forbidden:
                    violations.append(f"{path}:{node.lineno}: from {node.module} import ...")

    assert violations == []


def test_nutrition_record_verification_prompts_do_not_trust_recent_chat():
    supervisor_prompt = multiturn_chat_prompt()
    management_prompt = nutrition_management_agent_prompt()
    recommendation_prompt = nutrition_recommendation_agent_prompt()

    assert "Recent chat is not an authoritative source for current nutrition records" in supervisor_prompt
    assert "do not answer from recent_chat" in supervisor_prompt
    assert "delegate to delegate_to_nutrition_management_agent" in supervisor_prompt
    assert "context.recent_diet_recommendations" in supervisor_prompt
    assert "call get_nutrition_meal_record_list first" in management_prompt
    assert "Do not infer current records from recent chat" in management_prompt
    assert "Use context.recent_diet_recommendations before search_nutrition_food_candidates" in management_prompt
    assert "prefer meal-like foods over snacks or beverages" in recommendation_prompt


def test_agent_app_health_reports_native_runtime():
    response = TestClient(native_agent_main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "runtime": "langgraph_native"}


def test_agent_app_async_task_status_endpoint_reports_counts():
    response = TestClient(native_agent_main.app).get("/agent/async/tasks/status", headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert isinstance(payload["counts"], dict)
    assert isinstance(payload["active_count"], int)
    assert payload["active_count"] >= 0
    assert payload["active_statuses"] == ["callback_sent", "pending", "running"]


def test_agent_app_async_task_observability_endpoints_report_task_metadata():
    native_agent_main.Base.metadata.create_all(bind=native_agent_main.engine)
    request_id = f"observability-{uuid4().hex}"
    with native_agent_main.Session(native_agent_main.engine) as session:
        task, _created = enqueue_async_task(
            session,
            request_id=request_id,
            task_type="missed_dose",
            payload={"dose_event_id": 12, "phr_patient_key": "secret-phr-key"},
            callback_context={"job_id": 12},
        )
        task.status = DEAD
        task.attempts = 3
        task.last_error = "callback failed"
        session.commit()

    client = TestClient(native_agent_main.app)
    list_response = client.get("/agent/async/tasks", params={"status": DEAD, "limit": 20}, headers=internal_auth_headers())
    dead_response = client.get("/agent/async/tasks/dead", headers=internal_auth_headers())
    detail_response = client.get(f"/agent/async/tasks/{request_id}", headers=internal_auth_headers())
    missing_response = client.get("/agent/async/tasks/not-a-real-task", headers=internal_auth_headers())

    assert list_response.status_code == 200
    list_payload = list_response.json()
    matching = [item for item in list_payload["tasks"] if item["request_id"] == request_id]
    assert len(matching) == 1
    assert matching[0]["status"] == DEAD
    assert matching[0]["attempts"] == 3
    assert matching[0]["last_error"] == "callback failed"
    assert matching[0]["age_seconds"] >= 0
    assert matching[0]["runtime_seconds"] is None
    assert matching[0]["is_locked"] is False
    assert matching[0]["is_retry_due"] is True
    assert matching[0]["payload_keys"] == ["dose_event_id", "phr_patient_key"]
    assert matching[0]["payload_parse_error"] is False
    assert matching[0]["callback_context"] == {"job_id": 12}
    assert "secret-phr-key" not in json.dumps(list_payload, ensure_ascii=False)

    assert dead_response.status_code == 200
    assert any(item["request_id"] == request_id for item in dead_response.json()["tasks"])
    assert detail_response.status_code == 200
    assert detail_response.json()["task"]["request_id"] == request_id
    assert missing_response.status_code == 404


def test_agent_app_lifespan_does_not_start_embedded_worker_by_default(monkeypatch):
    calls: list[bool] = []
    reset_calls: list[bool] = []

    def fake_worker(stop_event, _orchestrator):
        calls.append(stop_event.is_set())

    monkeypatch.setenv("AGENT_EMBEDDED_WORKER_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(native_agent_main, "async_task_worker", fake_worker)
    monkeypatch.setattr(native_agent_main, "reset_running_async_tasks", lambda _session: reset_calls.append(True) or 0)

    try:
        with TestClient(native_agent_main.app):
            pass
    finally:
        get_settings.cache_clear()

    assert calls == []
    assert reset_calls == []


def test_agent_app_lifespan_can_start_embedded_worker_for_dev(monkeypatch):
    calls: list[bool] = []
    reset_calls: list[bool] = []

    def fake_worker(stop_event, _orchestrator):
        calls.append(stop_event.is_set())

    monkeypatch.setenv("AGENT_EMBEDDED_WORKER_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(native_agent_main, "async_task_worker", fake_worker)
    monkeypatch.setattr(native_agent_main, "reset_running_async_tasks", lambda _session: reset_calls.append(True) or 0)

    try:
        with TestClient(native_agent_main.app):
            pass
    finally:
        get_settings.cache_clear()

    assert calls == [False]
    assert reset_calls == [True]


def test_agent_app_requires_internal_token_when_configured(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_TOKEN", "agent-test-token")
    get_settings.cache_clear()
    client = TestClient(native_agent_main.app)

    try:
        missing = client.get("/agent/model-config")
        wrong = client.get("/agent/model-config", headers={"X-Internal-Api-Token": "wrong-token"})
        accepted = client.get("/agent/model-config", headers={"X-Internal-Api-Token": "agent-test-token"})
        health = client.get("/health")
    finally:
        get_settings.cache_clear()

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert accepted.status_code == 200
    assert health.status_code == 200


def test_agent_app_legacy_sync_daily_and_missed_endpoints_are_removed():
    client = TestClient(native_agent_main.app)

    daily_response = client.post("/agent/daily-patterns", json=build_daily_pattern().model_dump(mode="json"), headers=internal_auth_headers())
    missed_response = client.post("/agent/missed-dose-events", json=build_missed_payload().model_dump(mode="json"), headers=internal_auth_headers())

    assert daily_response.status_code == 404
    assert missed_response.status_code == 404


def test_missed_dose_hybrid_payload_uses_only_explicit_generated_message():
    output = {
        "pattern_code": "B",
        "pattern_confidence": 0.8,
        "tone_key": "persuasion",
        "message": "일반 응답 메시지는 미복용 멘트가 아닙니다.",
        "patient_message": "일반 환자 메시지도 미복용 멘트가 아닙니다.",
        "missed_dose_hybrid": {
            "reason": "설득형 톤에 맞춘 짧은 루틴 회복 문구입니다.",
            "tone_key": "persuasion",
            "message": "이 필드도 무시합니다.",
            "patient_message": "이 필드도 무시합니다.",
            "generated_message": "오늘 루틴을 다시 이어가볼까요?",
            "safety_notes": ["no_medication_name", "no_diagnosis", "non_directive"],
        },
    }

    payload = missed_dose_hybrid_payload(output)

    assert list(payload)[:2] == ["reason", "generated_message"]
    assert payload["reason"] == "설득형 톤에 맞춘 짧은 루틴 회복 문구입니다."
    assert payload["pattern_code"] == "B"
    assert payload["tone_key"] == "persuasion"
    assert payload["generated_message"] == "오늘 루틴을 다시 이어가볼까요?"
    assert payload["safety_notes"] == ["no_medication_name", "no_diagnosis", "non_directive"]
    assert payload["generated_message"] != output["message"]
    assert payload["generated_message"] != output["patient_message"]


def test_tool_catalog_can_be_exposed_as_mcp_tools_list():
    tools = ToolCatalog.available_tools_payload()
    payload = mcp_tools_list(tools)

    assert "tools" in payload
    assert {tool["name"] for tool in payload["tools"]} >= {"update_medication_dose_event_status", "get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"}
    for tool in payload["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]
        assert tool["outputSchema"]["type"] == "object"
        assert tool["args_schema"] == tool["inputSchema"]
        assert {"domain", "source_repo", "source_path", "source_tool_name", "mutability", "risk_level"} <= set(tool)
        assert tool["_meta"]["domain"] == tool["domain"]


def test_model_visible_tool_contract_does_not_expose_legacy_names():
    tools_payload = json.dumps(ToolCatalog.available_tools_payload(), ensure_ascii=False)
    delegation_payload = json.dumps(delegation_tools_payload(), ensure_ascii=False)
    prompt_payload = "\n".join(
        [
            multiturn_chat_prompt(),
            nutrition_management_agent_prompt(),
            nutrition_recommendation_agent_prompt(),
        ]
    )
    visible_payload = "\n".join([tools_payload, delegation_payload, prompt_payload])
    violations = [
        legacy_name
        for legacy_name in sorted(LEGACY_TOOL_NAMES)
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(legacy_name)}(?![A-Za-z0-9_])", visible_payload)
    ]

    assert violations == []


def test_legacy_tool_name_replacement_keeps_canonical_names_stable():
    assert replace_legacy_tool_names("record_meal should be hidden") == "create_nutrition_meal_record should be hidden"
    assert replace_legacy_tool_names(CREATE_NUTRITION_MEAL_RECORD) == CREATE_NUTRITION_MEAL_RECORD


def test_tool_result_round_trips_through_mcp_shape():
    result = ToolCallResult(
        tool_name="update_medication_dose_event_status",
        status="success",
        response={"status": "taken", "message": "복용 완료로 기록했습니다."},
        idempotency_key="trace:update_medication_dose_event_status",
    )

    mcp_result = mcp_result_from_tool_result(result)
    restored = tool_result_from_mcp_result("update_medication_dose_event_status", mcp_result)

    assert mcp_result["isError"] is False
    assert mcp_result["content"][0]["type"] == "text"
    assert mcp_result["structuredContent"]["tool_name"] == "update_medication_dose_event_status"
    assert restored == result


def test_http_status_tool_error_preserves_public_detail_code_and_redacts_private_detail():
    public_response = httpx.Response(
        422,
        json={"detail": "invalid_date_format"},
        request=httpx.Request("POST", "http://system.test/api/agent/nutrition/meals"),
    )
    private_response = httpx.Response(
        422,
        json={"detail": "pytest private detail peanut allergy token=secret-value"},
        request=httpx.Request("POST", "http://system.test/api/agent/nutrition/meals"),
    )

    public_result = http_status_tool_error_result(
        httpx.HTTPStatusError("unprocessable", request=public_response.request, response=public_response),
        tool_name="create_nutrition_meal_record",
        trace_id="trace-http-public",
        elapsed_ms=7,
    )
    private_result = http_status_tool_error_result(
        httpx.HTTPStatusError("unprocessable", request=private_response.request, response=private_response),
        tool_name="create_nutrition_meal_record",
        trace_id="trace-http-private",
        elapsed_ms=9,
    )
    public_mcp = mcp_result_from_tool_result(public_result)
    private_mcp = mcp_result_from_tool_result(private_result)
    rendered_private = json.dumps(private_mcp, ensure_ascii=False)

    assert public_result.status == "error"
    assert public_result.error == "invalid_date_format"
    assert public_result.response["status_code"] == 422
    assert public_mcp["structuredContent"]["error"] == "invalid_date_format"
    assert public_mcp["structuredContent"]["response"]["detail"]["type"] == "clinical_text"
    assert private_result.error.startswith("clinical text redacted")
    assert "pytest private detail" not in rendered_private
    assert "secret-value" not in rendered_private


def test_mcp_agent_tool_executor_calls_tools_call_json_rpc():
    server = RecordingMcpServer()
    executor = McpAgentToolExecutor(server=server)

    result = asyncio.run(
        executor.execute_tool_call(
            {"name": "update_medication_dose_event_status", "arguments": {"dose_event_id": 12}},
            trace_id="trace-mcp",
            source_event_type="multiturn_chat",
            payload={"patient_id": "demo-patient"},
        )
    )

    assert result.tool_name == "update_medication_dose_event_status"
    assert result.status == "success"
    assert server.requests[0]["request"]["method"] == MCP_METHOD_TOOLS_CALL
    assert server.requests[0]["request"]["params"] == {"name": "update_medication_dose_event_status", "arguments": {"dose_event_id": 12}}
    assert server.requests[0]["trace_id"] == "trace-mcp"
    assert server.requests[0]["source_event_type"] == "multiturn_chat"


def test_agent_app_mcp_tools_list_endpoint_reports_context_allowed_catalog():
    request = mcp_json_rpc_request(MCP_METHOD_TOOLS_LIST, request_id="tools-list")

    response = TestClient(native_agent_main.app).post("/agent/mcp", json=request, headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "tools-list"
    tool_names = {tool["name"] for tool in payload["result"]["tools"]}
    assert payload["result"]["source_event_type"] == "mcp"
    assert {
        "get_pro_ctcae_questionnaire",
        "search_nutrition_food_candidates",
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "get_nutrition_meal_record_list",
        "get_nutrition_daily_summary",
        "upsert_nutrition_preference_fact",
        "get_nutrition_preference_summary",
    } <= tool_names
    assert "update_medication_dose_event_status" not in tool_names
    assert "get_medication_side_effect_assessment" not in tool_names
    assert "propose_notification_policy" not in tool_names

    context_request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_LIST,
        {"_meta": {"source_event_type": "multiturn_chat"}},
        request_id="tools-list-multiturn",
    )
    context_response = TestClient(native_agent_main.app).post("/agent/mcp", json=context_request, headers=internal_auth_headers())

    assert context_response.status_code == 200
    context_payload = context_response.json()
    assert context_payload["result"]["source_event_type"] == "multiturn_chat"
    context_tools = {tool["name"]: tool for tool in context_payload["result"]["tools"]}
    assert {"propose_notification_policy", "propose_system_policy"} <= set(context_tools)
    assert "update_medication_dose_event_status" not in context_tools
    assert "get_medication_side_effect_assessment" not in context_tools
    assert "get_pro_ctcae_questionnaire" not in context_tools
    policy_meta = context_tools["propose_notification_policy"]["_meta"]
    assert policy_meta["execution_mode"] == "deferred_confirmation"
    assert policy_meta["requires_human_handoff"] is True
    assert policy_meta["handoff_gate"] == "high_risk_policy_change"


def test_tool_call_validation_accepts_tool_name_alias():
    output = {
        "message": "음식 정보를 먼저 찾아볼게요.",
        "tool_calls": [
            {
                "tool_name": "search_nutrition_food_candidates",
                "args": {"query": "마라탕"},
            }
        ],
    }

    validate_llm_output("system_guidance", output, {})
    calls = normalize_tool_calls(output)

    assert calls == [
        {
            "tool_name": "search_nutrition_food_candidates",
            "args": {"query": "마라탕"},
            "name": "search_nutrition_food_candidates",
            "arguments": {"query": "마라탕"},
        }
    ]


def test_tool_calls_payload_keeps_food_search_meal_type_hint():
    candidates = [
        {"food_ref_id": f"food-{index}", "food_name": f"food {index}", "nutrients": {}}
        for index in range(8)
    ]

    payload = tool_calls_payload(
        [
            {
                "name": "search_nutrition_food_candidates",
                "arguments": {"query": "pizza", "limit": 6, "meal_type": "dinner"},
            }
        ],
        [
            ToolCallResult(
                tool_name="search_nutrition_food_candidates",
                status="success",
                response={"success": True, "query": "pizza", "candidates": candidates},
            )
        ],
    )

    assert payload["food_searches"][0]["query"] == "pizza"
    assert payload["food_searches"][0]["meal_type"] == "dinner"
    assert payload["food_searches"][0]["limit"] == 6


def test_agent_app_mcp_direct_call_enforces_default_tool_allowlist():
    request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_CALL,
        {"name": "update_medication_dose_event_status", "arguments": {"dose_event_id": 12}},
        request_id="blocked-tool",
    )

    response = TestClient(native_agent_main.app).post("/agent/mcp", json=request, headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    result = payload["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["tool_name"] == "update_medication_dose_event_status"
    assert result["structuredContent"]["status"] == "error"
    assert result["structuredContent"]["error"] == "tool_permission_denied"
    assert result["structuredContent"]["response"]["source_event_type"] == "mcp"


def test_specialist_source_event_types_enforce_tool_boundaries():
    meal_call = {
        "name": CREATE_NUTRITION_MEAL_RECORD,
        "arguments": {"meal_type": "lunch", "foods": [{"food_name": "rice"}]},
    }
    supervisor_denial = validate_tool_permission(meal_call, source_event_type=SOURCE_MULTITURN_CHAT, payload={})

    assert supervisor_denial == f"{CREATE_NUTRITION_MEAL_RECORD} is not allowed for {SOURCE_MULTITURN_CHAT}"
    assert validate_tool_permission(meal_call, source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT, payload={}) is None

    recommendation_call = {
        "name": GET_NUTRITION_RECOMMENDATION_CANDIDATES,
        "arguments": {"constraints": {"sodium": "low"}},
    }

    assert validate_tool_permission(recommendation_call, source_event_type=SOURCE_NUTRITION_RECOMMENDATION_AGENT, payload={}) is None
    assert (
        validate_tool_permission(recommendation_call, source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT, payload={})
        == f"{GET_NUTRITION_RECOMMENDATION_CANDIDATES} is not allowed for {SOURCE_NUTRITION_MANAGEMENT_AGENT}"
    )

    dose_call = {"name": UPDATE_MEDICATION_DOSE_EVENT_STATUS, "arguments": {"dose_event_id": 12}}
    dose_payload = {"context": {"today_dose_events": [{"dose_event_id": 12}]}}

    assert (
        validate_tool_permission(dose_call, source_event_type=SOURCE_MULTITURN_CHAT, payload=dose_payload)
        == f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is not allowed for {SOURCE_MULTITURN_CHAT}"
    )
    assert validate_tool_permission(dose_call, source_event_type=SOURCE_MEDICATION_AGENT, payload=dose_payload) is None

    for query_tool_name in (GET_MEDICATION_DOSE_STATUS, GET_SIDE_EFFECT_HISTORY):
        query_call = {"name": query_tool_name, "arguments": {}}
        assert (
            validate_tool_permission(query_call, source_event_type=SOURCE_MULTITURN_CHAT, payload={})
            == f"{query_tool_name} is not allowed for {SOURCE_MULTITURN_CHAT}"
        )
        assert validate_tool_permission(query_call, source_event_type=SOURCE_MEDICATION_AGENT, payload={}) is None
        assert async_continuation_type([query_call]) == ""

    assert (
        validate_tool_permission(dose_call, source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT, payload=dose_payload)
        == f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is not allowed for {SOURCE_NUTRITION_MANAGEMENT_AGENT}"
    )


def test_agent_app_mcp_allows_nutrition_tools():
    denial = validate_tool_permission(
        {
            "name": "create_nutrition_meal_record",
            "arguments": {
                "meal_type": "lunch",
                "foods": [{"food_name": "짜장면", "nutrients": {"sodium": 1200}}],
            },
        },
        source_event_type="mcp",
        payload={},
    )

    assert denial is None

    preference_denial = validate_tool_permission(
        {
            "name": "upsert_nutrition_preference_fact",
            "arguments": {"predicate": "dislikes", "object_label": "짜장면"},
        },
        source_event_type="mcp",
        payload={},
    )

    assert preference_denial is None
    assert validate_tool_permission(
        {"name": "update_nutrition_meal_record", "arguments": {"meal_id": 1, "meal_type": "dinner"}},
        source_event_type="mcp",
        payload={},
    ) is None
    assert validate_tool_permission(
        {"name": "delete_nutrition_meal_record", "arguments": {"meal_id": 1}},
        source_event_type="mcp",
        payload={},
    ) is None
    assert validate_tool_permission(
        {"name": "update_nutrition_food_record", "arguments": {"meal_id": 1, "food_id": 10, "portion": "half"}},
        source_event_type="mcp",
        payload={},
    ) is None
    assert validate_tool_permission(
        {"name": "delete_nutrition_food_record", "arguments": {"meal_id": 1, "food_id": 10}},
        source_event_type="mcp",
        payload={},
    ) is None


def test_rule_based_provider_splits_explicit_nutrition_preferences_by_entity():
    result = asyncio.run(
        RuleBasedProvider().generate_json(
            "",
            {
                "response_mode": "multiturn_chat",
                "message": "나는 짜장면 싫어하고 땅콩 알레르기가 있어",
            },
        )
    )

    calls = result["tool_calls"]
    by_label = {call["arguments"]["object_label"]: call["arguments"]["predicate"] for call in calls}

    assert by_label["짜장면"] == "dislikes"
    assert by_label["땅콩"] == "allergic_to"


def test_async_request_id_prefers_conversation_id_over_reset_prone_job_id():
    context = AgentCallbackContext(
        app_base_url="http://system",
        notification_id=3,
        job_id=1,
        conversation_id="missed-dose-1-abc123",
    )

    assert native_agent_main._request_id("missed_dose", context) == "missed_dose:conversation:missed-dose-1-abc123"


def test_agent_app_daily_pattern_endpoint_is_system_compatible(monkeypatch):
    provider = NativeFakeProvider()
    tool_executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor)

    response = asyncio.run(orchestrator.invoke("daily_pattern", build_daily_pattern().model_dump(mode="json")))

    payload = response.model_dump(mode="json")
    assert payload["agent_name"] == "daily_pattern_agent"
    assert payload["decision_type"] == "tool_call"
    assert payload["structured_payload"]["tools_executed"] is False
    assert payload["structured_payload"]["policy_confirmation_required"] is True
    assert payload["structured_payload"]["tool_results"][0]["tool_name"] == "propose_notification_policy"
    assert payload["structured_payload"]["tool_results"][0]["status"] == "skipped"
    assert payload["structured_payload"]["tool_results"][0]["response"]["human_handoff_required"] is True
    assert payload["structured_payload"]["tool_results"][0]["response"]["handoff_gate"] == "high_risk_policy_change"
    assert tool_executor.calls == []


def test_agent_app_missed_dose_endpoint_is_system_compatible(monkeypatch):
    provider = NativeFakeProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NativeFakeToolExecutor())

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    payload = response.model_dump(mode="json")
    assert payload["agent_name"] == "missed_dose_coach"
    assert payload["decision_type"] == "missed_dose_assessment"
    assert payload["requires_conversation_alert"] is True
    assert payload["structured_payload"]["dose_event_id"] == 12
    assert "Always include missed_dose_hybrid.generated_message" in provider.seen_prompts[0]
    assert "Put missed_dose_hybrid.reason before generated_message" in provider.seen_prompts[0]
    assert '"reason":"<why this expression fits the tone/context>","generated_message"' in provider.seen_prompts[0]
    assert "45 characters or fewer" in provider.seen_prompts[0]


def test_missed_dose_output_validator_fails_when_hybrid_required(monkeypatch):
    provider = InvalidMissedDoseHybridProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NativeFakeToolExecutor())
    payload = build_missed_payload().model_copy(
        update={
            "adherence_pattern_context": {"pattern_code": "B"},
            "tone_policy_context": {"tone_key": "persuasion"},
        }
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(orchestrator.invoke("missed_dose", payload.model_dump(mode="json")))

    assert exc_info.value.error_type == "llm_output_validation_failed"
    assert exc_info.value.agent_name == "missed_dose_coach"
    assert exc_info.value.decision_type == "missed_dose_assessment"


def test_missed_dose_tool_permission_blocks_mark_taken(monkeypatch):
    provider = UnsafeMissedDoseToolProvider()
    tool_executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor)

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    structured = response.structured_payload
    assert structured["tool_call"]["name"] == "update_medication_dose_event_status"
    assert structured["tool_results"][0]["tool_name"] == "update_medication_dose_event_status"
    assert structured["tool_results"][0]["status"] == "error"
    assert structured["tool_results"][0]["error"] == "tool_permission_denied"
    assert "missed_dose" in structured["tool_results"][0]["response"]["source_event_type"]
    assert tool_executor.calls == []


def test_agent_app_multiturn_blocks_direct_medication_tool_call(monkeypatch):
    provider = NativeFakeProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)

    response = client.post("/agent/multiturn-chat", json=build_taken_chat_request().model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    structured = payload["structured_payload"]
    assert payload["decision_type"] == "tool_call"
    assert structured["routing_mode"] == "direct_tool"
    assert structured["tool_call"]["name"] == "update_medication_dose_event_status"
    assert structured["tool_results"][0]["status"] == "error"
    assert structured["tool_results"][0]["error"] == "tool_permission_denied"
    assert structured["tool_results"][0]["response"]["source_event_type"] == SOURCE_MULTITURN_CHAT
    assert tool_executor.calls == []
    assert "delegate_to_medication_agent" in provider.bound_tool_names
    assert {
        "update_medication_dose_event_status",
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    }.isdisjoint(provider.bound_tool_names)

def test_agent_app_multiturn_delegates_medication_without_losing_mark_taken_permission(monkeypatch):
    provider = NativeDelegatingMedicationProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)

    response = client.post("/agent/multiturn-chat", json=build_taken_chat_request().model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_name"] == "system_event_agent"
    assert payload["decision_type"] == "tool_call"
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["delegated_agent"] == "medication_agent"
    assert payload["structured_payload"]["delegated_by"] == "system_event_agent"
    assert payload["structured_payload"]["supervisor_agent"] == "system_event_agent"
    assert payload["structured_payload"]["specialist_agent"] == "medication_agent"
    assert payload["structured_payload"]["executed_by"] == "system_event_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == "update_medication_dose_event_status"
    assert payload["structured_payload"]["tool_call"]["name"] == "update_medication_dose_event_status"
    assert payload["structured_payload"]["tool_results"][0]["tool_name"] == "update_medication_dose_event_status"
    assert payload["structured_payload"]["tool_results"][0]["status"] == "success"
    assert payload["structured_payload"]["tool_results"][0]["response"]["status"] == "taken"
    assert tool_executor.calls[0]["name"] == "update_medication_dose_event_status"
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_MEDICATION_AGENT
    assert [seen["response_mode"] for seen in provider.seen_payloads] == ["multiturn_chat", "medication_chat"]
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert "delegate_to_medication_agent" in supervisor_tools
    assert {"propose_notification_policy", "propose_system_policy"} <= supervisor_tools
    assert {
        "update_medication_dose_event_status",
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    }.isdisjoint(supervisor_tools)
    assert {
        "update_medication_dose_event_status",
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
        "get_medication_dose_status",
        "get_side_effect_history",
    } <= specialist_tools
    assert "get_nutrition_recommendation_candidates" not in specialist_tools


def test_agent_app_multiturn_delegates_nutrition_management_tools(monkeypatch):
    provider = NativeDelegatingNutritionManagementProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="삶은 계란 먹었어",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )

    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert payload["agent_name"] == "system_event_agent"
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["specialist_agent"] == "nutrition_management_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_management_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == "search_nutrition_food_candidates"
    assert tool_executor.calls[0]["name"] == "search_nutrition_food_candidates"
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_MANAGEMENT_AGENT
    assert {"delegate_to_nutrition_management_agent", "delegate_to_nutrition_recommendation_agent"} <= supervisor_tools
    assert not {
        "search_nutrition_food_candidates",
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "get_nutrition_recommendation_candidates",
    } & supervisor_tools
    assert {
        "search_nutrition_food_candidates",
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "get_nutrition_daily_summary",
    } <= specialist_tools
    assert "get_nutrition_recommendation_candidates" not in specialist_tools


def test_agent_app_multiturn_delegated_nutrition_food_update_reaches_supervisor_final(monkeypatch):
    provider = NativeMultiStepNutritionFoodUpdateProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="점심에 탕수육이 아니라 꿔바로우 먹었어. 바꿔줘",
        current_time=datetime(2026, 4, 20, 13, 20),
        context={"recent_chat": []},
    )

    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["agent_name"] == "system_event_agent"
    assert payload["human_summary"] == "점심 식사 기록에서 탕수육을 꿔바로우로 수정했어요."
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["specialist_agent"] == "nutrition_management_agent"
    assert payload["structured_payload"]["final_answer_source"] == "model_output"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_management_agent"
    assert [call["name"] for call in payload["structured_payload"]["specialist_tool_calls"]] == [
        "get_nutrition_meal_record_list",
        "search_nutrition_food_candidates",
        "update_nutrition_food_record",
    ]
    assert [call["name"] for call in tool_executor.calls] == [
        "get_nutrition_meal_record_list",
        "search_nutrition_food_candidates",
        "update_nutrition_food_record",
    ]
    assert provider.bound_tool_history[0] and "delegate_to_nutrition_management_agent" in provider.bound_tool_history[0]
    assert provider.bound_tool_history[1:] and all("update_nutrition_food_record" in names for names in provider.bound_tool_history[1:4])


def test_agent_app_multiturn_delegates_nutrition_recommendation_tools(monkeypatch):
    provider = NativeDelegatingNutritionRecommendationProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="저녁 뭐 먹을까?",
        current_time=datetime(2026, 4, 20, 18, 30),
        context={"recent_chat": []},
    )

    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert payload["agent_name"] == "system_event_agent"
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["specialist_agent"] == "nutrition_recommendation_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_recommendation_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == "get_nutrition_recommendation_candidates"
    assert tool_executor.calls[0]["name"] == "get_nutrition_recommendation_candidates"
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_RECOMMENDATION_AGENT
    assert not {
        "search_nutrition_food_candidates",
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "get_nutrition_recommendation_candidates",
    } & supervisor_tools
    assert {"search_nutrition_food_candidates", "get_nutrition_meal_record_list", "get_nutrition_daily_summary", "get_nutrition_preference_summary", "get_nutrition_recommendation_candidates"} <= specialist_tools
    assert not {
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "upsert_nutrition_preference_fact",
    } & specialist_tools


def test_specialist_state_graph_stops_at_common_tool_loop_limit():
    provider = NativeLoopLimitProvider()
    tool_executor = NativeFakeToolExecutor()

    graph_runner = ToolChatAgentGraph(
        provider=provider,
        tool_runtime=ToolRuntime(tool_executor),
        agent_name="nutrition_management_agent",
        prompt=nutrition_management_agent_prompt(),
        response_mode="nutrition_management_chat",
        decision_type="tool_call",
        tool_names=("search_nutrition_food_candidates",),
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
    )

    response = asyncio.run(
        graph_runner.invoke(
            "trace-specialist-loop-limit",
            {
                "patient_id": "demo-patient",
                "phr_patient_key": "phr-demo",
                "message": "계란 정보를 확인해줘",
                "context": {},
            },
        )
    )
    structured = response.structured_payload
    assert structured["routing_mode"] == "specialist_max_iterations"
    assert structured["tool_loop_mode"] == "langgraph_state_graph"
    assert len(structured["tool_calls"]) == SPECIALIST_TOOL_LOOP_LIMIT
    assert len(tool_executor.calls) == SPECIALIST_TOOL_LOOP_LIMIT
    assert provider.chat_model_call_count == 1
    assert structured["pending_tool_calls"][0]["name"] == "search_nutrition_food_candidates"
    assert "도구 실행 단계" in response.human_summary


def test_agent_app_multiturn_delegates_side_effect_continuation_to_medication_agent(monkeypatch):
    provider = NativeSideEffectLookupProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)

    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="I feel nauseous. Could it be the medication?",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )
    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    structured = payload["structured_payload"]
    assert payload["decision_type"] == "async_continuation_requested"
    assert structured["routing_mode"] == "delegated_agent"
    assert structured["async_continuation_type"] == "side_effect_assessment"
    assert structured["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert [call["name"] for call in structured["specialist_tool_calls"]] == ["get_medication_side_effect_assessment"]
    assert tool_executor.calls == []
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert "delegate_to_medication_agent" in supervisor_tools
    assert {"get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"}.isdisjoint(supervisor_tools)
    assert {"get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"} <= specialist_tools

    continuation = request.model_copy(deep=True)
    continuation.context = {
        "execute_async_continuation": True,
        "async_tool_calls": structured["tool_calls"],
    }
    continuation_response = client.post("/agent/multiturn-chat", json=continuation.model_dump(mode="json"), headers=internal_auth_headers())

    assert continuation_response.status_code == 200
    continuation_payload = continuation_response.json()
    continuation_structured = continuation_payload["structured_payload"]
    assert continuation_payload["decision_type"] == "side_effect_assessment"
    assert continuation_structured["routing_mode"] == "delegated_agent"
    assert continuation_structured["delegation_reason"] == "async_medication_continuation"
    assert continuation_structured["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert [call["name"] for call in continuation_structured["specialist_tool_calls"]] == [
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    ]
    assert [result["tool_name"] for result in continuation_structured["tool_results"]] == [
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    ]
    assert [call["name"] for call in tool_executor.calls] == [
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    ]
    assert all(call["_source_event_type"] == SOURCE_MEDICATION_AGENT for call in tool_executor.calls)

def test_agent_app_multiturn_uses_provider_for_general_recent_chat_reply(monkeypatch):
    provider = NativeRecentChatProvider()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NativeFakeToolExecutor()),
    )
    client = TestClient(native_agent_main.app)
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="나 아까 문제가 있다고했었나?",
        current_time=datetime(2026, 5, 22, 4, 51),
        context={
            "recent_chat": [
                {"role": "assistant", "content": "아침 08:00 당뇨약 복약을 놓치신 것으로 확인되었습니다. 현재 상태를 알려주세요."},
                {"role": "user", "content": "속이 메스꺼운데 약때문일까?"},
                {"role": "assistant", "content": "증상에 맞는 PRO-CTCAE 자기보고 문항을 준비했습니다. 메스꺼움"},
                {"role": "user", "content": "1번 문항: 자주 있다"},
                {"role": "user", "content": "2번 문항: 보통이다"},
                {"role": "user", "content": "나 아까 문제가 있다고했었나?"},
            ]
        },
    )

    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision_type"] == "system_guidance"
    assert payload["structured_payload"]["final_answer_source"] == "model_output"
    assert "요청을 확인했습니다" not in payload["human_summary"]
    assert "메스꺼" in payload["human_summary"]
    assert "1번 자주 있다" in payload["human_summary"]
    assert provider.seen_payloads[0]["context"]["recent_chat"][1]["content"] == "속이 메스꺼운데 약때문일까?"
    assert "answer ordinary follow-up" in provider.seen_prompts[0]


def test_rule_based_provider_is_test_only_for_runtime_selection(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "rule_based")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="test-only"):
            create_llm_provider()
    finally:
        get_settings.cache_clear()


def test_ae_tool_call_from_lookup_normalizes_generic_phr_effect_to_pro_ctcae_symptom():
    result = ToolCallResult(
        tool_name="get_medication_side_effect_assessment",
        status="success",
        response={
            "suspected": True,
            "matched_effects": ["당뇨약: 주의사항 관련 증상"],
            "matched_items": ["당뇨약"],
            "severity": "high",
            "evidence": "당뇨약 주의사항 키워드(메스꺼)",
            "recommendation": "증상 문항 확인이 필요합니다.",
        },
    )
    tool_call = ae_tool_call_from_lookup(
        {
            "name": "get_medication_side_effect_assessment",
            "arguments": {"symptom_text": "속이 메스꺼운데 약때문일까?"},
        },
        result,
        {"message": "속이 메스꺼운데 약때문일까?"},
    )

    assert tool_call["name"] == "get_pro_ctcae_questionnaire"
    assert tool_call["arguments"]["symptom_normalize"] == "메스꺼움"


def test_rule_based_provider_requires_repeated_daily_pattern_before_policy_tool_call():
    provider = RuleBasedProvider()

    daily_output = asyncio.run(provider.generate_json("", {**build_daily_pattern().model_dump(mode="json"), "response_mode": "daily_pattern_analysis"}))
    repeated_daily_output = asyncio.run(
        provider.generate_json("", {**build_repeated_daily_pattern().model_dump(mode="json"), "response_mode": "daily_pattern_analysis"})
    )
    chat_output = asyncio.run(provider.generate_json("", build_taken_chat_request().model_dump(mode="json") | {"response_mode": "multiturn_chat"}))
    side_effect_output = asyncio.run(
        provider.generate_json(
            "",
            {
                **build_missed_payload().model_dump(mode="json"),
                "response_mode": "missed_dose_coaching",
                "chat_context": [{"role": "user", "content": "속이 메스꺼운데 약 때문일까?"}],
            },
        )
    )

    assert daily_output["tool_calls"] == []
    assert daily_output["repeated_missed_slots"] == []
    assert repeated_daily_output["tool_calls"][0]["name"] == "propose_notification_policy"
    assert repeated_daily_output["tool_calls"][0]["arguments"]["slot_label"] == "아침 08:00"
    assert "tool_call" not in chat_output
    assert "tool_calls" not in chat_output
    assert "tool_call" not in side_effect_output
    assert "tool_calls" not in side_effect_output
    assert side_effect_output["side_effect_signal"] is False


def test_rule_based_provider_returns_general_chat_when_no_tool_needed():
    provider = RuleBasedProvider()
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="아까 뭔말했더라",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={
            "recent_chat": [
                {"role": "user", "content": "속이 메스꺼운데 약때문일까?"},
                {"role": "user", "content": "1번 문항: 자주 있다"},
            ]
        },
    )

    chat_output = asyncio.run(provider.generate_json("", request.model_dump(mode="json") | {"response_mode": "multiturn_chat"}))

    assert "tool_call" not in chat_output
    assert "속이 메스꺼운데 약때문일까?" in chat_output["advice"]
    assert "1번 문항: 자주 있다" in chat_output["advice"]


def test_rule_based_provider_answers_nutrition_and_medication_chat_together():
    provider = RuleBasedProvider()
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="오늘 점심이 짰고 당 수치가 걱정돼요. 복약과 저녁 식사를 같이 조정해줘.",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"nutrition": {"today_summary": {"status": "exceeded"}}},
    )

    chat_output = asyncio.run(provider.generate_json("", request.model_dump(mode="json") | {"response_mode": "multiturn_chat"}))

    assert "tool_call" not in chat_output
    assert "tool_calls" not in chat_output
    assert "영양과 복약" in chat_output["advice"]
    assert "저녁" in chat_output["advice"]
    assert "처방된 복약 시간" in chat_output["advice"]


def test_notification_policy_deltas_fill_daily_pattern_source():
    policies = _notification_policy_deltas(
        {
            "slot_label": "아침 08:00",
            "extra_reminders": 2,
            "interval_minutes": 10,
            "effective_start_date": "2026-04-20",
            "effective_end_date": "2026-04-27",
            "reason": "아침 미복용 패턴",
        },
        source_event_type="daily_pattern",
    )

    assert len(policies) == 1
    assert policies[0].source == "pattern_analysis"


def test_notification_policy_deltas_normalize_daily_pattern_source_alias():
    policies = _notification_policy_deltas(
        {
            "slot_label": "아침 08:00",
            "extra_reminders": 2,
            "interval_minutes": 20,
            "effective_start_date": "2026-04-21",
            "effective_end_date": "2026-05-20",
            "reason": "daily_pattern_analysis:complete_miss_all_slots",
            "source": "ai_adherence_analysis",
        },
        source_event_type="daily_pattern",
    )

    assert len(policies) == 1
    assert policies[0].source == "pattern_analysis"


def build_daily_pattern() -> DailyMedicationPattern:
    return DailyMedicationPattern(
        patient_id="demo-patient",
        date=date(2026, 4, 20),
        schedule_slots=["아침 08:00"],
        dose_events=[
            DosePatternEvent(
                dose_event_id=12,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 20, 8, 0),
                status="missed",
            )
        ],
        slot_summaries=[
            SlotAdherenceSummary(
                slot_label="아침 08:00",
                scheduled_count=1,
                taken_count=0,
                missed_count=1,
                miss_rate=1.0,
            )
        ],
    )


def build_repeated_daily_pattern() -> DailyMedicationPattern:
    return DailyMedicationPattern(
        patient_id="demo-patient",
        date=date(2026, 4, 21),
        window_start_date=date(2026, 4, 20),
        window_end_date=date(2026, 4, 21),
        window_days=2,
        observed_day_count=2,
        schedule_slots=["아침 08:00"],
        dose_events=[
            DosePatternEvent(
                dose_event_id=12,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 20, 8, 0),
                status="missed",
            ),
            DosePatternEvent(
                dose_event_id=13,
                medication_name="혈압약",
                slot_label="아침 08:00",
                scheduled_for=datetime(2026, 4, 21, 8, 0),
                status="missed",
            ),
        ],
        slot_summaries=[
            SlotAdherenceSummary(
                slot_label="아침 08:00",
                scheduled_count=2,
                taken_count=0,
                missed_count=2,
                miss_rate=1.0,
            )
        ],
    )


def build_missed_payload() -> MissedDoseEventPayload:
    return MissedDoseEventPayload(
        patient_id="demo-patient",
        dose_event_id=12,
        medication_name="혈압약",
        slot_label="아침 08:00",
        scheduled_for=datetime(2026, 4, 20, 8, 0),
        detected_at=datetime(2026, 4, 20, 9, 30),
        recent_slot_summaries=[],
        chat_context=[],
    )


def build_taken_chat_request() -> MultiturnChatRequest:
    return MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="아침 약은 방금 복용했어. 기록해줘.",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={
            "schedule_slots": ["아침 08:00"],
            "today_dose_events": [
                {
                    "dose_event_id": 12,
                    "medication_name": "혈압약",
                    "slot_label": "아침 08:00",
                    "scheduled_for": "2026-04-20T08:00:00",
                    "status": "missed",
                }
            ],
        },
    )
