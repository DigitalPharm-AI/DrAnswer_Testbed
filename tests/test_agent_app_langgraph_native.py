from __future__ import annotations

import ast
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

import agent_app.main as native_agent_main
from agent_app.agents.multiturn_output import (
    normalize_mutation_confirmation_output,
)
from agent_app.agents.tool_chat import AGENT_TOOL_LOOP_LIMIT, ToolChatAgentGraph
from agent_app.errors import AgentExecutionError
from agent_app.jobs.tasks import DEAD, enqueue_async_task
from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    human_payload_from_messages,
    langchain_tool_name,
    tool_results_from_messages,
)
from agent_app.llm.prompts import (
    medication_agent_prompt,
    multiturn_chat_prompt,
    mutation_confirmation_prompt,
    mutation_confirmation_reply_prompt,
    nutrition_management_agent_prompt,
    nutrition_recommendation_agent_prompt,
)
from agent_app.llm.responses import missed_dose_hybrid_payload
from agent_app.llm.validation import validate_llm_output
from agent_app.orchestration.continuation import continuation_type
from agent_app.orchestration.delegation import delegation_tools_payload
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.providers.deterministic_test import DeterministicTestProvider
from agent_app.providers.factory import create_llm_provider
from agent_app.tools.calling import normalize_tool_calls
from agent_app.tools.executor import McpAgentToolExecutor
from agent_app.tools.mcp_server import http_status_tool_error_result
from agent_app.tools.policy import (
    notification_policy_deltas,
)
from agent_app.tools.protocol import (
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    mcp_json_rpc_request,
    mcp_result_from_tool_result,
    mcp_tools_list,
    tool_result_from_mcp_result,
)
from agent_app.tools.results import tool_calls_payload
from agent_app.tools.runtime import ToolRuntime
from agent_app.tools.side_effects import (
    ae_tool_calls_from_lookup,
    positive_side_effect_lookup,
)
from shared.schemas import (
    AgentCallbackContext,
    MultiturnChatRequest,
    ToolCallResult,
)
from shared.settings import get_settings
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    ALL_TOOL_NAMES,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_NUTRITION_MEAL_RECORD,
    DELEGATION_TOOL_NAMES,
    GET_MEDICATION_DOSE_STATUS,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_SIDE_EFFECT_HISTORY,
    REQUEST_RECORD_APPROVAL,
    SOURCE_MCP,
    SOURCE_MEDICATION_AGENT,
    SOURCE_MULTITURN_CHAT,
    SOURCE_NUTRITION_MANAGEMENT_AGENT,
    SOURCE_NUTRITION_RECOMMENDATION_AGENT,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)
from shared.tool_permissions import validate_tool_permission
from tests.support.agent_scenarios import (
    NativeDelegatingMedicationProvider,
    NativeFakeToolExecutor,
    NativeRecentChatProvider,
    build_daily_pattern,
    build_missed_payload,
    build_repeated_daily_pattern,
    build_taken_chat_request,
)
from tests.support.llm import NativeChatProvider, NativeProviderChatModel


def _chat_result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])


class NativeFakeProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.seen_prompts: list[str] = []
        self.bound_tool_names: list[str] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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
        if response_mode == "missed_dose_message_generation":
            return {
                "generated_message": (
                    "오늘 복약이 어려우셨나요? 현재 상태를 알려주세요."
                ),
            }
        if response_mode == "final_answer":
            return {"message": "요청 처리 결과를 확인했습니다."}
        if "복용" in str(user_payload.get("message", "")):
            return {
                "message": "복약 완료를 기록하겠습니다.",
                "tool_call": {
                    "name": "update_medication_dose_event_status",
                    "arguments": {
                        "dose_event_id": "dose-event-12",
                        "reason": "patient_reported_taken",
                    },
                },
            }
        return {"advice": "현재 복약 상태를 확인했습니다.", "observations": ["추가 도구 실행은 필요하지 않습니다."]}


class NativeDelegatingNutritionManagementProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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
                    "arguments": {
                        "food_queries": ["삶은 계란"]
                    },
                },
            }
        return {"advice": "확인했습니다."}


class NativeDelegatingNutritionRecommendationProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.bound_tool_names: list[str] = []
        self.bound_tool_history: list[list[str]] = []

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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


class NativeMultiStepNutritionFoodUpdateChatModel(NativeProviderChatModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        payload = human_payload_from_messages(messages)
        tool_results = tool_results_from_messages(messages)
        tool_result_names = [result.tool_name for result in tool_results]
        self.provider.bound_tool_history.append([langchain_tool_name(tool) for tool in self.bound_tools])

        if payload.get("response_mode") == "final_answer":
            summary = "점심 식사 기록에서 탕수육을 꿔바로우로 수정했어요."
            return _chat_result(
                AIMessage(
                    content=summary,
                    response_metadata={"model_output": {"message": summary}},
                )
            )
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
                        [
                            {
                                "name": "search_nutrition_food_candidates",
                                "arguments": {
                                    "food_queries": [
                                        "꿔바로우"
                                    ]
                                },
                            }
                        ],
                        content="새 음식의 영양 정보를 확인하겠습니다.",
                        model_output={"message": "새 음식의 영양 정보를 확인하겠습니다."},
                    )
                )
            if tool_result_names[-1] == "search_nutrition_food_candidates":
                return _chat_result(
                    ai_message_from_tool_calls(
                        [
                            {
                                "name": REQUEST_RECORD_APPROVAL,
                                "arguments": {
                                    "action_name": "update_nutrition_food_record",
                                    "record_arguments": {
                                        "meal_id": "meal-101",
                                        "food_id": "food-202",
                                        "food_ref_id": "guobaorou",
                                        "food_name": "꿔바로우",
                                        "portion": {"amount": 1, "unit": "serving"},
                                        "nutrients": {"calories": 360, "protein": 18},
                                    },
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

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
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


class NativeLoopLimitChatModel(NativeProviderChatModel):
    async def _agenerate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager=None, **kwargs: Any) -> ChatResult:
        tool_call = {
            "name": "search_nutrition_food_candidates",
            "arguments": {"food_queries": ["계란"]},
        }
        return _chat_result(
            ai_message_from_tool_calls(
                [tool_call],
                content="음식 정보를 계속 확인합니다.",
                model_output={"message": "음식 정보를 계속 확인합니다.", "tool_call": tool_call},
            )
        )


class BlankToolFinalizingProvider(NativeChatProvider):
    def __init__(self, *, blank_response_mode: str = "multiturn_chat") -> None:
        self.blank_response_mode = blank_response_mode

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        response_mode = user_payload.get("response_mode")
        if response_mode == "multiturn_chat":
            return {
                "message": "영양 관리 에이전트가 확인하겠습니다.",
                "tool_call": {
                    "name": "delegate_to_nutrition_management_agent",
                    "arguments": {
                        "task": "search food candidates",
                        "reason": "patient asked about a food",
                    },
                },
            }
        if response_mode == "nutrition_management_chat":
            return {
                "message": "음식 후보를 확인하겠습니다.",
                "tool_call": {
                    "name": "search_nutrition_food_candidates",
                    "arguments": {
                        "food_queries": ["삶은 계란"]
                    },
                },
            }
        if response_mode == "final_answer":
            return {"message": "   "}
        raise AssertionError(f"unexpected_response_mode:{response_mode}")

    async def finalize_tool_results(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        tool_results: list[ToolCallResult],
    ) -> dict[str, Any]:
        response_mode = str(user_payload.get("response_mode") or "")
        if response_mode == self.blank_response_mode:
            return {"message": "   "}
        if response_mode == "nutrition_management_chat":
            return {"message": "삶은 계란 후보를 확인했습니다."}
        raise AssertionError(f"unexpected_response_mode:{response_mode}")


class InvalidMissedDoseHybridProvider(NativeChatProvider):
    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert (
            user_payload["response_mode"]
            == "missed_dose_message_generation"
        )
        return {
            "patient_message": "확인했습니다.",
            "likely_reason": "unknown",
        }


class UnsafeMissedDoseToolProvider(NativeChatProvider):
    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert (
            user_payload["response_mode"]
            == "missed_dose_message_generation"
        )
        return {
            "generated_message": "복용 완료를 기록하겠습니다.",
            "tool_call": {
                "name": "update_medication_dose_event_status",
                "arguments": {"dose_event_id": "dose-event-12"},
            },
        }


class UnsafeMissedDoseMessageProvider(NativeChatProvider):
    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert (
            user_payload["response_mode"]
            == "missed_dose_message_generation"
        )
        return {
            "generated_message": "혈압약은 반드시 복용하세요.",
        }


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


def invoke_multiturn_graph(request: MultiturnChatRequest):
    return asyncio.run(
        native_agent_main.orchestrator.invoke(
            "multiturn_chat",
            request.model_dump(mode="json"),
        )
    )


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
    assert "context.structured_response_context is present" in supervisor_prompt
    assert "not a new generic message" in supervisor_prompt
    assert "delegate to delegate_to_nutrition_management_agent" in supervisor_prompt
    assert "context.recent_diet_recommendations" in supervisor_prompt
    assert "call get_nutrition_meal_record_list first" in management_prompt
    assert "Do not infer current records from recent chat" in management_prompt
    assert "Use context.recent_diet_recommendations before search_nutrition_food_candidates" in management_prompt
    assert "Call search_nutrition_food_candidates exactly once" in management_prompt
    assert "food_queries in the same order" in management_prompt
    assert "the Tool owns search expansion, deduplication, and ranking" in management_prompt
    assert "prefer meal-like foods over snacks or beverages" in recommendation_prompt


def test_multiturn_prompt_routes_global_notification_control_to_application_ui():
    supervisor_prompt = multiturn_chat_prompt()

    assert "turn all medication reminders and missed-dose AI notifications on or off globally" in supervisor_prompt
    assert "do not call propose_notification_policy, propose_system_policy, or any other tool" in supervisor_prompt
    assert "change the setting directly in the application" in supervisor_prompt
    assert "Notification policy creation is not supported in chat" in supervisor_prompt
    assert "never call propose_notification_policy from this supervisor" in supervisor_prompt
    assert "may only modify an existing active policy" in supervisor_prompt
    assert "Call get_notification_policies with active_only=true first" in supervisor_prompt
    assert (
        "call request_record_approval with action_name "
        "change_notification_policy"
    ) in supervisor_prompt
    assert "do not call the write Tool before the user confirms it" in supervisor_prompt


def test_record_confirmation_prompts_keep_button_labels_out_of_message_text():
    prompts = [
        multiturn_chat_prompt(),
        mutation_confirmation_prompt(),
        mutation_confirmation_reply_prompt(),
        medication_agent_prompt(),
        nutrition_management_agent_prompt(),
    ]

    assert all(
        "button label" in prompt
        and ("pseudo-button" in prompt or "structured selections" in prompt)
        for prompt in prompts
    )
    assert "Do not repeat action_label or cancel_label" in mutation_confirmation_prompt()
    assert '{"message":' in mutation_confirmation_prompt()
    assert "do not return a question field" in mutation_confirmation_prompt()


def test_mutation_confirmation_provider_question_is_normalized_to_message():
    assert normalize_mutation_confirmation_output(
        {"question": "이 식사 기록을 저장할까요?"}
    ) == {"message": "이 식사 기록을 저장할까요?"}


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
    native_agent_main.verify_agent_schema_current(native_agent_main.engine)
    request_id = f"observability-{uuid4().hex}"
    with native_agent_main.Session(native_agent_main.engine) as session:
        task, _created = enqueue_async_task(
            session,
            request_id=request_id,
            task_type="missed_dose",
            payload={"dose_event_id": 12, "patient_id": "patient-observability"},
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
    assert matching[0]["payload_keys"] == ["dose_event_id", "patient_id"]
    assert matching[0]["payload_parse_error"] is False
    assert matching[0]["callback_context"] == {"job_id": 12}
    assert "patient-observability" not in json.dumps(list_payload, ensure_ascii=False)

    assert dead_response.status_code == 200
    assert any(item["request_id"] == request_id for item in dead_response.json()["tasks"])
    assert detail_response.status_code == 200
    assert detail_response.json()["task"]["request_id"] == request_id
    assert missing_response.status_code == 404


def test_agent_app_lifespan_does_not_start_embedded_worker_by_default(monkeypatch):
    calls: list[bool] = []
    reset_calls: list[bool] = []

    def fake_worker(stop_event, _orchestrator, _backend_queries):
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

    def fake_worker(stop_event, _orchestrator, _backend_queries):
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
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["outputSchema"]["type"] == "object"
        assert tool["args_schema"] == tool["inputSchema"]
        assert {"domain", "source_repo", "source_path", "source_tool_name", "mutability", "risk_level"} <= set(tool)
        assert tool["_meta"]["domain"] == tool["domain"]

    preference_tool = next(tool for tool in payload["tools"] if tool["name"] == UPSERT_NUTRITION_PREFERENCE_FACT)
    assert "Never store inability to consume as avoids_by_preference" in preference_tool["description"]
    predicate_schema = preference_tool["inputSchema"]["properties"]["predicate"]
    assert predicate_schema["description"] == "Hard restrictions must not use a preference predicate."
    assert "cannot_consume" in predicate_schema["enum"]
    side_effect_tool = next(
        tool
        for tool in payload["tools"]
        if tool["name"] == "get_medication_side_effect_assessment"
    )
    side_effect_schema = side_effect_tool["inputSchema"]
    assert side_effect_schema["required"] == ["symptom_mentions"]
    assert set(side_effect_schema["properties"]) == {
        "symptom_mentions",
        "medication_name",
    }
    assert side_effect_schema["properties"]["symptom_mentions"][
        "maxItems"
    ] == 5
    assert side_effect_schema["properties"]["symptom_mentions"][
        "items"
    ]["additionalProperties"] is False
    food_search_tool = next(
        tool
        for tool in payload["tools"]
        if tool["name"]
        == "search_nutrition_food_candidates"
    )
    food_search_schema = food_search_tool["inputSchema"]
    assert food_search_schema["required"] == [
        "food_queries"
    ]
    assert set(food_search_schema["properties"]) == {
        "food_queries",
        "limit_per_query",
        "meal_type",
    }
    assert food_search_schema["properties"][
        "food_queries"
    ]["maxItems"] == 8
    assert food_search_schema["properties"][
        "food_queries"
    ]["uniqueItems"] is True


def test_model_visible_tool_contract_uses_only_canonical_names():
    tools = ToolCatalog.available_tools_payload()
    delegation_tools = delegation_tools_payload()

    assert {tool["name"] for tool in tools} <= ALL_TOOL_NAMES
    assert {tool["name"] for tool in delegation_tools} == DELEGATION_TOOL_NAMES
    assert {tool["name"] for tool in delegation_tools}.isdisjoint(ALL_TOOL_NAMES)
    assert all(
        tool["inputSchema"]["additionalProperties"] is False
        for tool in delegation_tools
    )


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
        request=httpx.Request(
            "POST",
            "http://system.test/agent/sync/record-change",
        ),
    )
    private_response = httpx.Response(
        422,
        json={"detail": "pytest private detail peanut allergy token=secret-value"},
        request=httpx.Request(
            "POST",
            "http://system.test/agent/sync/record-change",
        ),
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
            {
                "name": "update_medication_dose_event_status",
                "arguments": {"dose_event_id": "dose-event-12"},
            },
            trace_id="trace-mcp",
            source_event_type="multiturn_chat",
            payload={"patient_id": "demo-patient"},
        )
    )

    assert result.tool_name == "update_medication_dose_event_status"
    assert result.status == "success"
    assert server.requests[0]["request"]["method"] == MCP_METHOD_TOOLS_CALL
    assert server.requests[0]["request"]["params"] == {
        "name": "update_medication_dose_event_status",
        "arguments": {"dose_event_id": "dose-event-12"},
    }
    assert server.requests[0]["trace_id"] == "trace-mcp"
    assert server.requests[0]["source_event_type"] == "multiturn_chat"


def test_agent_app_mcp_tools_list_endpoint_is_always_generic_read_only():
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
        "get_nutrition_meal_record_list",
        "get_nutrition_daily_summary",
        "get_nutrition_preference_summary",
        "get_nutrition_recommendation_candidates",
    } <= tool_names
    assert not {
        "request_record_approval",
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
        "upsert_nutrition_preference_fact",
    }.intersection(tool_names)
    assert "update_medication_dose_event_status" not in tool_names
    assert "get_medication_side_effect_assessment" not in tool_names
    assert "propose_notification_policy" not in tool_names

    spoofed_meta_request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_LIST,
        {"_meta": {"source_event_type": SOURCE_NUTRITION_MANAGEMENT_AGENT}},
        request_id="tools-list-spoofed-meta",
    )
    spoofed_param_request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_LIST,
        {"source_event_type": SOURCE_MEDICATION_AGENT},
        request_id="tools-list-spoofed-param",
    )

    for spoofed_request in (spoofed_meta_request, spoofed_param_request):
        spoofed_response = TestClient(native_agent_main.app).post(
            "/agent/mcp",
            json=spoofed_request,
            headers=internal_auth_headers(),
        )

        assert spoofed_response.status_code == 200
        spoofed_payload = spoofed_response.json()
        assert spoofed_payload["result"]["source_event_type"] == SOURCE_MCP
        spoofed_tools = {
            tool["name"] for tool in spoofed_payload["result"]["tools"]
        }
        assert spoofed_tools == tool_names
        assert CREATE_NUTRITION_MEAL_RECORD not in spoofed_tools
        assert UPDATE_MEDICATION_DOSE_EVENT_STATUS not in spoofed_tools
        assert "get_medication_side_effect_assessment" not in spoofed_tools


def test_tool_call_validation_accepts_canonical_name_with_native_args():
    output = {
        "message": "음식 정보를 먼저 찾아볼게요.",
        "tool_calls": [
            {
                "name": "search_nutrition_food_candidates",
                "args": {"food_queries": ["마라탕"]},
            }
        ],
    }

    validate_llm_output("system_guidance", output, {})
    calls = normalize_tool_calls(output)

    assert calls == [
        {
            "name": "search_nutrition_food_candidates",
            "args": {"food_queries": ["마라탕"]},
            "arguments": {
                "food_queries": ["마라탕"]
            },
        }
    ]


def test_tool_calls_payload_keeps_food_search_meal_type_hint():
    candidates = [{"food_ref_id": f"food-{index}", "food_name": f"food {index}", "nutrients": {}} for index in range(8)]
    second_candidates = [
        {
            "food_ref_id": "food-side",
            "food_name": "side food",
            "nutrients": {},
        }
    ]

    payload = tool_calls_payload(
        [
            {
                "name": "search_nutrition_food_candidates",
                "arguments": {
                    "food_queries": ["pizza"],
                    "limit_per_query": 6,
                    "meal_type": "dinner",
                },
            }
        ],
        [
            ToolCallResult(
                tool_name="search_nutrition_food_candidates",
                status="success",
                response={
                    "success": True,
                    "search_groups": [
                        {
                            "query": "pizza",
                            "candidates": candidates,
                        },
                        {
                            "query": "side",
                            "candidates": second_candidates,
                        },
                    ],
                    "limit_per_query": 6,
                },
            )
        ],
    )

    assert payload["food_searches"][0]["query"] == "pizza"
    assert payload["food_searches"][0]["meal_type"] == "dinner"
    assert payload["food_searches"][0]["limit"] == 6
    assert payload["food_searches"][1]["query"] == "side"
    assert payload["food_candidates"] == candidates
    assert payload["food_selection_progress"] == {
        "current_group": 1,
        "total_groups": 2,
        "query": "pizza",
    }


def test_agent_app_mcp_direct_call_enforces_default_tool_allowlist():
    request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_CALL,
        {
            "name": "update_medication_dose_event_status",
            "arguments": {"dose_event_id": "dose-event-12"},
        },
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


def test_agent_app_mcp_direct_nutrition_write_is_read_only_denied():
    request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_CALL,
        {
            "name": CREATE_NUTRITION_MEAL_RECORD,
            "arguments": {
                "meal_type": "lunch",
                "foods": [{"food_name": "삶은 계란"}],
            },
            "_meta": {
                "source_event_type": SOURCE_NUTRITION_MANAGEMENT_AGENT,
            },
        },
        request_id="blocked-generic-mcp-write",
    )

    response = TestClient(native_agent_main.app).post(
        "/agent/mcp",
        json=request,
        headers=internal_auth_headers(),
    )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert (
        result["structuredContent"]["tool_name"]
        == CREATE_NUTRITION_MEAL_RECORD
    )
    assert result["structuredContent"]["error"] == "tool_permission_denied"
    assert (
        result["structuredContent"]["response"]["source_event_type"]
        == SOURCE_MCP
    )


def test_specialist_source_event_types_enforce_tool_boundaries():
    meal_call = {
        "name": CREATE_NUTRITION_MEAL_RECORD,
        "arguments": {
            "meal_type": "lunch",
            "foods": [{"food_name": "rice", "nutrients": {}}],
        },
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

    dose_call = {
        "name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "arguments": {"dose_event_id": "dose-event-12"},
    }
    dose_payload = {
        "context": {
            "today_dose_events": [{"dose_event_id": "dose-event-12"}]
        }
    }

    assert (
        validate_tool_permission(dose_call, source_event_type=SOURCE_MULTITURN_CHAT, payload=dose_payload)
        == f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is not allowed for {SOURCE_MULTITURN_CHAT}"
    )
    assert validate_tool_permission(dose_call, source_event_type=SOURCE_MEDICATION_AGENT, payload=dose_payload) is None

    for query_tool_name in (GET_MEDICATION_DOSE_STATUS, GET_SIDE_EFFECT_HISTORY):
        query_call = {"name": query_tool_name, "arguments": {}}
        assert validate_tool_permission(query_call, source_event_type=SOURCE_MULTITURN_CHAT, payload={}) == f"{query_tool_name} is not allowed for {SOURCE_MULTITURN_CHAT}"
        assert validate_tool_permission(query_call, source_event_type=SOURCE_MEDICATION_AGENT, payload={}) is None
        assert continuation_type([query_call]) == ""

    assert (
        validate_tool_permission(dose_call, source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT, payload=dose_payload)
        == f"{UPDATE_MEDICATION_DOSE_EVENT_STATUS} is not allowed for {SOURCE_NUTRITION_MANAGEMENT_AGENT}"
    )


@pytest.mark.parametrize(
    "tool_name",
    [
        CREATE_NUTRITION_MEAL_RECORD,
        UPSERT_NUTRITION_PREFERENCE_FACT,
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
    ],
)
def test_agent_app_generic_mcp_source_is_read_only(tool_name):
    denial = validate_tool_permission(
        {"name": tool_name, "arguments": {}},
        source_event_type="mcp",
        payload={},
    )

    assert denial == f"{tool_name} is not allowed for mcp"


def test_deterministic_provider_splits_explicit_nutrition_preferences_by_entity():
    result = DeterministicTestProvider().model_output(
        {
            "response_mode": "multiturn_chat",
            "message": "나는 짜장면 싫어하고 땅콩 알레르기가 있어",
        }
    )

    calls = result["tool_calls"]
    by_label = {call["arguments"]["object_label"]: call["arguments"]["predicate"] for call in calls}

    assert by_label["짜장면"] == "dislikes"
    assert by_label["땅콩"] == "allergic_to"


def test_async_request_id_prefers_stable_job_id():
    context = AgentCallbackContext(
        app_base_url="http://system",
        notification_id=3,
        job_id=1,
    )

    assert native_agent_main._request_id("missed_dose", context) == "missed_dose:job:1"


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
    assert payload["structured_payload"]["feedback_category"] == "B"
    assert payload["structured_payload"]["feedback_tone"] == "persuasion"
    assert payload["structured_payload"]["tools_executed"] is False
    assert payload["structured_payload"]["tool_execution_mode"] == "none"
    assert len(provider.seen_prompts) == 1
    assert len(provider.seen_payloads) == 1
    assert (
        provider.seen_payloads[0]["response_mode"]
        == "missed_dose_message_generation"
    )
    serialized_payload = json.dumps(
        provider.seen_payloads[0],
        ensure_ascii=False,
    )
    assert "demo-patient" not in serialized_payload
    assert "혈압약" not in serialized_payload
    assert "dose_event_id" not in serialized_payload
    assert "복약 루틴을 함께 맞춰봐요" not in serialized_payload


def test_missed_dose_fails_closed_when_feedback_context_is_missing(monkeypatch):
    provider = InvalidMissedDoseHybridProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NativeFakeToolExecutor())
    payload = build_missed_payload().model_copy(
        update={
            "adherence_pattern_context": {},
            "tone_policy_context": {},
        }
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(orchestrator.invoke("missed_dose", payload.model_dump(mode="json")))

    assert exc_info.value.error_type == "missed_dose_policy_context_invalid"
    assert exc_info.value.agent_name == "missed_dose_coach"
    assert exc_info.value.decision_type == "missed_dose_assessment"


def test_missed_dose_rejects_invalid_llm_schema_without_fallback():
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider=InvalidMissedDoseHybridProvider(),
        tool_executor=NativeFakeToolExecutor(),
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            orchestrator.invoke(
                "missed_dose",
                build_missed_payload().model_dump(mode="json"),
            )
        )

    assert exc_info.value.error_type == "llm_output_validation_failed"


def test_missed_dose_rejects_unsafe_llm_message_without_fallback():
    orchestrator = AgentLangGraphNativeOrchestrator(
        provider=UnsafeMissedDoseMessageProvider(),
        tool_executor=NativeFakeToolExecutor(),
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            orchestrator.invoke(
                "missed_dose",
                build_missed_payload().model_dump(mode="json"),
            )
        )

    assert exc_info.value.error_type == "llm_output_validation_failed"


def test_missed_dose_rejects_tool_calls_without_executing_them(monkeypatch):
    provider = UnsafeMissedDoseToolProvider()
    tool_executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor)

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            orchestrator.invoke(
                "missed_dose",
                build_missed_payload().model_dump(mode="json"),
            )
        )

    assert exc_info.value.error_type == "llm_output_validation_failed"
    assert tool_executor.calls == []


def test_agent_app_multiturn_blocks_direct_medication_tool_call(monkeypatch):
    provider = NativeFakeProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))

    payload = invoke_multiturn_graph(build_taken_chat_request()).model_dump(mode="json")
    structured = payload["structured_payload"]
    assert payload["agent_name"] == "multiturn_chat_agent"
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

    payload = invoke_multiturn_graph(build_taken_chat_request()).model_dump(mode="json")
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_required"
    assert payload["structured_payload"]["routing_mode"] == "mutation_confirmation_required"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["supervisor_agent"] == "multiturn_chat_agent"
    assert payload["structured_payload"]["executed_by"] == "multiturn_chat_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == REQUEST_RECORD_APPROVAL
    assert payload["structured_payload"]["tool_results"][0]["tool_name"] == REQUEST_RECORD_APPROVAL
    assert payload["structured_payload"]["tool_results"][0]["status"] == "confirmation_required"
    assert payload["structured_payload"]["mutation_confirmation"]["action_name"] == UPDATE_MEDICATION_DOSE_EVENT_STATUS
    assert tool_executor.calls[0]["name"] == REQUEST_RECORD_APPROVAL
    assert tool_executor.calls[0]["arguments"]["action_name"] == UPDATE_MEDICATION_DOSE_EVENT_STATUS
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_MEDICATION_AGENT
    assert [seen.get("response_mode") for seen in provider.seen_payloads] == [
        "multiturn_chat",
        "medication_chat",
        None,
    ]
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert "delegate_to_medication_agent" in supervisor_tools
    assert {
        "propose_system_policy",
        "get_notification_policies",
        "request_record_approval",
    } <= supervisor_tools
    assert "propose_notification_policy" not in supervisor_tools
    assert "change_notification_policy" not in supervisor_tools
    assert {
        "update_medication_dose_event_status",
        "get_medication_side_effect_assessment",
        "get_pro_ctcae_questionnaire",
    }.isdisjoint(supervisor_tools)
    assert {
        "request_record_approval",
        "get_medication_side_effect_assessment",
        "get_medication_dose_status",
        "get_side_effect_history",
    } <= specialist_tools
    assert "get_pro_ctcae_questionnaire" not in specialist_tools
    assert "update_medication_dose_event_status" not in specialist_tools
    assert "get_nutrition_recommendation_candidates" not in specialist_tools


def test_agent_app_executes_approved_policy_change_in_supervisor(
    monkeypatch,
):
    class ApprovedPolicyProvider(NativeChatProvider):
        async def model_output(
            self,
            _system_prompt: str,
            user_payload: dict[str, Any],
        ) -> dict[str, Any]:
            if (
                user_payload.get("response_mode")
                == "approved_write_finalization"
            ):
                return {"message": "아침 알림 정책을 변경했어요."}
            raise AssertionError(
                "approved policy Tool must be forced before model routing"
            )

        async def finalize_tool_results(
            self,
            _system_prompt: str,
            _user_payload: dict[str, Any],
            tool_results: list[ToolCallResult],
        ) -> dict[str, Any]:
            assert [result.tool_name for result in tool_results] == [
                CHANGE_NOTIFICATION_POLICY
            ]
            return {"message": "아침 알림 정책을 변경했어요."}

    class ApprovedPolicyExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def execute_tool_call(
            self,
            tool_call,
            *,
            trace_id,
            source_event_type,
            payload,
        ):
            self.calls.append(
                {
                    **tool_call,
                    "_source_event_type": source_event_type,
                    "_payload": payload,
                }
            )
            return ToolCallResult(
                tool_name=CHANGE_NOTIFICATION_POLICY,
                status="success",
                response={
                    "success": True,
                    "result": {
                        "policy_id": "npol_approved",
                        "decision": "apply",
                        "version": 5,
                    },
                },
                idempotency_key="mutation:policy-confirmation",
            )

    executor = ApprovedPolicyExecutor()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(
            provider=ApprovedPolicyProvider(),
            tool_executor=executor,
        ),
    )
    request = build_taken_chat_request().model_copy(deep=True)
    request.message = "변경"
    request.context = {
        **request.context,
        "approved_user_action": {
            "action_name": CHANGE_NOTIFICATION_POLICY,
            "status": "confirmed",
            "arguments": {
                "approval_key": "apv_abcdefghijklmnopqrstuvwx",
            },
        },
    }

    response = invoke_multiturn_graph(request)

    assert response.human_summary == "아침 알림 정책을 변경했어요."
    assert len(executor.calls) == 1
    assert executor.calls[0]["name"] == CHANGE_NOTIFICATION_POLICY
    assert (
        executor.calls[0]["_source_event_type"]
        == SOURCE_MULTITURN_CHAT
    )
    assert executor.calls[0]["arguments"] == {
        "approval_key": "apv_abcdefghijklmnopqrstuvwx",
    }
    assert "apv_abcdefghijklmnopqrstuvwx" not in json.dumps(
        response.structured_payload,
        ensure_ascii=False,
    )


def test_agent_app_mutation_confirmation_stops_specialist_and_finishes_in_supervisor(monkeypatch):
    provider = NativeDelegatingMedicationProvider()

    class ConfirmationExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            self.calls.append({**tool_call, "_source_event_type": source_event_type})
            return ToolCallResult(
                tool_name=tool_call["name"],
                status="confirmation_required",
                response={
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                        "tool_call_id": tool_call.get("id", ""),
                        "action_fingerprint": "fingerprint-1",
                        "status": "pending",
                        "display": {
                            "title": "dose confirmation",
                            "question": "confirm dose update",
                        },
                    }
                },
            )

    executor = ConfirmationExecutor()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=executor),
    )
    payload = invoke_multiturn_graph(build_taken_chat_request()).model_dump(mode="json")
    structured = payload["structured_payload"]
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_required"
    assert structured["routing_mode"] == "mutation_confirmation_required"
    assert structured["mutation_confirmation_required"] is True
    assert "confirmation_id" not in structured["mutation_confirmation"]
    assert "approval_key" not in structured["mutation_confirmation"]
    assert structured["tool_results"][0]["status"] == "confirmation_required"
    assert executor.calls[0]["_source_event_type"] == SOURCE_MEDICATION_AGENT
    assert provider.chat_model_bound_tool_history[-1] == []


def test_agent_app_nutrition_preference_confirmation_stops_specialist_and_finishes_in_supervisor(monkeypatch):
    class NutritionPreferenceProvider(NativeChatProvider):
        def __init__(self) -> None:
            self.bound_tool_names: list[str] = []

        async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
            if user_payload.get("response_mode") == "multiturn_chat":
                return {
                    "message": "I will delegate the nutrition preference update.",
                    "tool_call": {
                        "name": "delegate_to_nutrition_management_agent",
                        "arguments": {
                            "task": "Record the explicit apple allergy.",
                            "reason": "The user stated a permanent nutrition constraint.",
                        },
                    },
                }
            if user_payload.get("response_mode") == "nutrition_management_chat":
                return {
                    "message": "I will prepare the allergy preference update.",
                    "tool_call": {
                        "name": UPSERT_NUTRITION_PREFERENCE_FACT,
                        "arguments": {
                            "predicate": "allergic_to",
                            "object_label": "apple",
                            "object_type": "ingredient",
                            "evidence_text": "I am allergic to apples.",
                        },
                    },
                }
            return {"message": "Please confirm the nutrition preference update below."}

    class ConfirmationExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            self.calls.append({**tool_call, "_source_event_type": source_event_type})
            return ToolCallResult(
                tool_name=tool_call["name"],
                status="confirmation_required",
                response={
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": UPSERT_NUTRITION_PREFERENCE_FACT,
                        "tool_call_id": tool_call.get("id", ""),
                        "action_fingerprint": "preference-fingerprint-1",
                        "status": "pending",
                        "display": {
                            "title": "nutrition preference confirmation",
                            "question": "confirm apple allergy",
                        },
                    }
                },
            )

    provider = NutritionPreferenceProvider()
    executor = ConfirmationExecutor()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=executor),
    )
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="What should I eat for dinner? I am allergic to apples.",
        current_time=datetime(2026, 4, 20, 18, 30),
        context={"recent_chat": []},
    )

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    structured = payload["structured_payload"]
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_required"
    assert structured["routing_mode"] == "mutation_confirmation_required"
    assert structured["mutation_confirmation"]["action_name"] == UPSERT_NUTRITION_PREFERENCE_FACT
    assert structured["tool_results"][0]["status"] == "confirmation_required"
    assert executor.calls[0]["name"] == UPSERT_NUTRITION_PREFERENCE_FACT
    assert executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_MANAGEMENT_AGENT
    assert provider.chat_model_bound_tool_history[-1] == []



@pytest.mark.parametrize("intent", ["confirm", "cancel", "unclear"])
def test_agent_app_interprets_pending_mutation_confirmation_reply_without_tools(monkeypatch, intent):
    class ConfirmationReplyProvider(NativeChatProvider):
        def __init__(self) -> None:
            self.seen_payloads: list[dict[str, Any]] = []
            self.bound_tool_names: list[str] = []

        async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
            self.seen_payloads.append(user_payload)
            if user_payload["response_mode"] == "final_answer":
                return {"message": f"confirmation reply: {intent}"}
            assert user_payload["response_mode"] == "mutation_confirmation_reply"
            return {"intent": intent, "message": f"confirmation reply: {intent}"}

    class NoToolExecutor:
        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            raise AssertionError("confirmation reply interpretation must not execute tools")

    provider = ConfirmationReplyProvider()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NoToolExecutor()),
    )
    request = build_taken_chat_request().model_copy(deep=True)
    request.message = "natural confirmation reply"
    request.context = {
        **request.context,
        "pending_mutation_confirmation": {
            "display": {"question": "Apply this dose update?"},
            "original_request": "I took my lunch dose.",
        },
    }

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_reply"
    assert payload["structured_payload"]["mutation_confirmation_reply"] == {"intent": intent}
    assert payload["structured_payload"]["tool_calls"] == []
    assert payload["structured_payload"]["tool_execution_mode"] == "none"
    assert provider.chat_model_bound_tool_history == [[]]


def test_agent_app_continues_new_request_after_pending_confirmation_reply_classification(monkeypatch):
    class NewRequestProvider(NativeChatProvider):
        def __init__(self) -> None:
            self.seen_payloads: list[dict[str, Any]] = []
            self.bound_tool_names: list[str] = []

        async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
            self.seen_payloads.append(user_payload)
            if user_payload.get("response_mode") == "mutation_confirmation_reply":
                return {"intent": "new_request", "message": "This is a new request."}
            return {"message": "Handled the independent request."}

    class NoToolExecutor:
        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            raise AssertionError("direct new request must not execute tools")

    provider = NewRequestProvider()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NoToolExecutor()),
    )
    request = build_taken_chat_request().model_copy(deep=True)
    request.message = "What time is it now?"
    request.context = {
        **request.context,
        "pending_mutation_confirmation": {
            "display": {"question": "Apply this dose update?"},
            "original_request": "I took my lunch dose.",
        },
    }

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    assert payload["decision_type"] == "system_guidance"
    assert payload["human_summary"] == "Handled the independent request."
    assert payload["structured_payload"]["mutation_confirmation_reply"] == {"intent": "new_request"}
    assert [item.get("response_mode") for item in provider.seen_payloads] == [
        "mutation_confirmation_reply",
        "multiturn_chat",
    ]
    assert "pending_mutation_confirmation" not in provider.seen_payloads[1]["context"]
    assert provider.seen_payloads[1]["context"]["pending_mutation_confirmation_reply_resolved"] == "new_request"
    assert provider.chat_model_bound_tool_history[0] == []
    assert "delegate_to_medication_agent" in provider.chat_model_bound_tool_history[1]


def test_agent_app_revises_pending_preference_confirmation_with_a_new_proposal(monkeypatch):
    class RevisionProvider(NativeChatProvider):
        def __init__(self) -> None:
            self.seen_payloads: list[dict[str, Any]] = []
            self.bound_tool_names: list[str] = []

        async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
            self.seen_payloads.append(user_payload)
            response_mode = user_payload.get("response_mode")
            if response_mode == "mutation_confirmation_reply":
                return {"intent": "revise", "message": "I will correct the proposal."}
            if response_mode == "multiturn_chat":
                revision = user_payload["context"]["mutation_confirmation_revision"]
                assert revision["display"]["target"] == "watermelon"
                assert revision["user_revision"] == "Apply it as a hard ingestion restriction."
                return {
                    "message": "I will delegate the corrected restriction.",
                    "tool_call": {
                        "name": "delegate_to_nutrition_management_agent",
                        "arguments": {
                            "task": "Replace the pending watermelon preference with a hard ingestion restriction.",
                            "reason": "The user corrected the pending classification.",
                        },
                    },
                }
            if response_mode == "nutrition_management_chat":
                assert user_payload["context"]["mutation_confirmation_revision"]["display"]["target"] == "watermelon"
                return {
                    "message": "I will prepare the corrected restriction.",
                    "tool_call": {
                        "name": UPSERT_NUTRITION_PREFERENCE_FACT,
                        "arguments": {
                            "predicate": "cannot_consume",
                            "object_label": "watermelon",
                            "object_type": "food",
                            "safety_level": "hard",
                            "evidence_text": "Apply it as a hard ingestion restriction.",
                        },
                    },
                }
            return {"message": "Please review the corrected restriction below."}

    class RevisionExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            self.calls.append({**tool_call, "_source_event_type": source_event_type})
            return ToolCallResult(
                tool_name=tool_call["name"],
                status="confirmation_required",
                response={
                    "mutation_confirmation": {
                        "confirmation_required": True,
                        "action_type": "agent_tool",
                        "action_name": UPSERT_NUTRITION_PREFERENCE_FACT,
                        "tool_call_id": tool_call.get("id", ""),
                        "action_fingerprint": "preference-revision-fingerprint",
                        "status": "pending",
                        "display": {
                            "title": "nutrition restriction confirmation",
                            "question": "confirm watermelon restriction",
                            "target": "watermelon",
                        },
                    }
                },
            )

    provider = RevisionProvider()
    executor = RevisionExecutor()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=executor),
    )
    request = build_taken_chat_request().model_copy(deep=True)
    request.message = "Apply it as a hard ingestion restriction."
    request.context = {
        **request.context,
        "pending_mutation_confirmation": {
            "display": {
                "title": "nutrition preference confirmation",
                "question": "Record watermelon as a preference-based avoid?",
                "target": "watermelon",
            },
            "original_request": "Record that I cannot eat watermelon.",
        },
    }

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    structured = payload["structured_payload"]
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_required"
    assert structured["mutation_confirmation_reply"] == {"intent": "revise"}
    assert structured["mutation_confirmation_revision"]["display"]["target"] == "watermelon"
    assert "confirmation_id" not in structured["mutation_confirmation"]
    assert "approval_key" not in structured["mutation_confirmation"]
    assert executor.calls[0]["name"] == UPSERT_NUTRITION_PREFERENCE_FACT
    assert executor.calls[0]["arguments"]["predicate"] == "cannot_consume"
    assert executor.calls[0]["arguments"]["safety_level"] == "hard"
    assert executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_MANAGEMENT_AGENT

def test_agent_app_empty_pending_confirmation_context_uses_standard_supervisor_route(monkeypatch):
    class StandardProvider(NativeChatProvider):
        def __init__(self) -> None:
            self.bound_tool_names: list[str] = []

        async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
            if user_payload["response_mode"] == "final_answer":
                return {"message": "standard response"}
            assert user_payload["response_mode"] == "multiturn_chat"
            return {"message": "standard response"}

    class NoToolExecutor:
        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            raise AssertionError("standard direct response must not execute tools")

    provider = StandardProvider()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NoToolExecutor()),
    )
    request = build_taken_chat_request().model_copy(deep=True)
    request.message = "hello"
    request.context = {**request.context, "pending_mutation_confirmation": {}}

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    assert payload["decision_type"] == "system_guidance"
    assert payload["human_summary"] == "standard response"
    assert "mutation_confirmation_reply" not in payload["structured_payload"]
    assert "delegate_to_medication_agent" in provider.chat_model_bound_tool_history[0]


def test_agent_app_rejects_unapplied_mutation_result_instead_of_claiming_success(monkeypatch):
    provider = NativeDelegatingMedicationProvider()

    class SkippedMutationExecutor:
        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            return ToolCallResult(
                tool_name=tool_call["name"],
                status="skipped",
                response={
                    "dose_event_id": "dose-event-12",
                    "status": "taken",
                    "message": "stale result from an earlier confirmation",
                },
                error="mutation_confirmation_applied",
            )

    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=SkippedMutationExecutor()),
    )
    with pytest.raises(AgentExecutionError) as exc_info:
        invoke_multiturn_graph(build_taken_chat_request())

    assert exc_info.value.error_type == "mutation_tool_not_applied"


def test_agent_app_multiturn_delegates_nutrition_management_tools(monkeypatch):
    provider = NativeDelegatingNutritionManagementProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="삶은 계란 먹었어",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["specialist_agent"] == "nutrition_management_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_management_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == "search_nutrition_food_candidates"
    assert tool_executor.calls[0]["name"] == "search_nutrition_food_candidates"
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_MANAGEMENT_AGENT
    assert {"delegate_to_nutrition_management_agent", "delegate_to_nutrition_recommendation_agent"} <= supervisor_tools
    assert (
        not {
            "search_nutrition_food_candidates",
            "create_nutrition_meal_record",
            "update_nutrition_meal_record",
            "delete_nutrition_meal_record",
            "update_nutrition_food_record",
            "delete_nutrition_food_record",
            "get_nutrition_recommendation_candidates",
        }
        & supervisor_tools
    )
    assert {
        "search_nutrition_food_candidates",
        "request_record_approval",
        "get_nutrition_daily_summary",
    } <= specialist_tools
    assert not {
        "create_nutrition_meal_record",
        "update_nutrition_meal_record",
        "delete_nutrition_meal_record",
        "update_nutrition_food_record",
        "delete_nutrition_food_record",
    }.intersection(specialist_tools)
    assert "get_nutrition_recommendation_candidates" not in specialist_tools


def test_agent_app_multiturn_delegated_nutrition_food_update_requests_supervisor_confirmation(monkeypatch):
    provider = NativeMultiStepNutritionFoodUpdateProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="점심에 탕수육이 아니라 꿔바로우 먹었어. 바꿔줘",
        current_time=datetime(2026, 4, 20, 13, 20),
        context={"recent_chat": []},
    )

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "mutation_confirmation_required"
    assert payload["structured_payload"]["routing_mode"] == "mutation_confirmation_required"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["mutation_confirmation"]["action_name"] == "update_nutrition_food_record"
    assert payload["structured_payload"]["finalization_mode"] == "confirmation_llm_without_tools"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_management_agent"
    assert [call["name"] for call in payload["structured_payload"]["specialist_tool_calls"]] == [
        "get_nutrition_meal_record_list",
        "search_nutrition_food_candidates",
        REQUEST_RECORD_APPROVAL,
    ]
    assert [call["name"] for call in tool_executor.calls] == [
        "get_nutrition_meal_record_list",
        "search_nutrition_food_candidates",
        REQUEST_RECORD_APPROVAL,
    ]
    assert tool_executor.calls[-1]["arguments"]["action_name"] == "update_nutrition_food_record"
    assert provider.bound_tool_history[0] and "delegate_to_nutrition_management_agent" in provider.bound_tool_history[0]
    assert provider.bound_tool_history[1:]
    assert all(
        "request_record_approval" in names
        and "update_nutrition_food_record" not in names
        for names in provider.bound_tool_history[1:4]
    )


def test_agent_app_multiturn_delegates_nutrition_recommendation_tools(monkeypatch):
    provider = NativeDelegatingNutritionRecommendationProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="저녁 뭐 먹을까?",
        current_time=datetime(2026, 4, 20, 18, 30),
        context={"recent_chat": []},
    )

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["structured_payload"]["routing_mode"] == "delegated_agent"
    assert payload["structured_payload"]["tool_loop_mode"] == "langgraph_state_graph"
    assert payload["structured_payload"]["specialist_agent"] == "nutrition_recommendation_agent"
    assert payload["structured_payload"]["supervisor_tool_calls"][0]["name"] == "delegate_to_nutrition_recommendation_agent"
    assert payload["structured_payload"]["specialist_tool_calls"][0]["name"] == "get_nutrition_recommendation_candidates"
    assert tool_executor.calls[0]["name"] == "get_nutrition_recommendation_candidates"
    assert tool_executor.calls[0]["_source_event_type"] == SOURCE_NUTRITION_RECOMMENDATION_AGENT
    assert (
        not {
            "search_nutrition_food_candidates",
            "create_nutrition_meal_record",
            "update_nutrition_meal_record",
            "delete_nutrition_meal_record",
            "update_nutrition_food_record",
            "delete_nutrition_food_record",
            "get_nutrition_recommendation_candidates",
        }
        & supervisor_tools
    )
    assert {
        "search_nutrition_food_candidates",
        "get_nutrition_meal_record_list",
        "get_nutrition_daily_summary",
        "get_nutrition_preference_summary",
        "get_nutrition_recommendation_candidates",
    } <= specialist_tools
    assert (
        not {
            "create_nutrition_meal_record",
            "update_nutrition_meal_record",
            "delete_nutrition_meal_record",
            "update_nutrition_food_record",
            "delete_nutrition_food_record",
            "upsert_nutrition_preference_fact",
        }
        & specialist_tools
    )


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
                "message": "계란 정보를 확인해줘",
                "context": {},
            },
        )
    )
    structured = response.structured_payload
    assert structured["routing_mode"] == "specialist_max_iterations"
    assert structured["tool_loop_mode"] == "langgraph_state_graph"
    assert len(structured["tool_calls"]) == AGENT_TOOL_LOOP_LIMIT
    # The graph can observe the model repeating the same call until the loop
    # guard fires, but the runtime executes an identical read only once.
    assert len(tool_executor.calls) == 1
    assert provider.chat_model_call_count == 1
    assert structured["pending_tool_calls"][0]["name"] == "search_nutrition_food_candidates"
    assert "도구 실행 단계" in response.human_summary


def test_specialist_tool_result_without_final_llm_answer_fails_closed():
    provider = BlankToolFinalizingProvider(
        blank_response_mode="nutrition_management_chat",
    )
    graph_runner = ToolChatAgentGraph(
        provider=provider,
        tool_runtime=ToolRuntime(NativeFakeToolExecutor()),
        agent_name="nutrition_management_agent",
        prompt=nutrition_management_agent_prompt(),
        response_mode="nutrition_management_chat",
        decision_type="tool_call",
        tool_names=("search_nutrition_food_candidates",),
        source_event_type=SOURCE_NUTRITION_MANAGEMENT_AGENT,
        defer_tool_continuation=False,
    )

    with pytest.raises(AgentExecutionError) as exc_info:
        asyncio.run(
            graph_runner.invoke(
                "trace-specialist-missing-final-answer",
                {
                    "patient_id": "demo-patient",
                    "message": "삶은 계란 정보를 확인해줘",
                    "context": {},
                },
            )
        )

    assert exc_info.value.error_type == "llm_final_answer_missing"


def test_specialist_routes_required_food_portion_to_input_box_without_llm():
    class InputRequiredExecutor:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def execute_tool_call(
            self,
            tool_call: dict[str, Any],
            *,
            trace_id: str,
            source_event_type: str,
            payload: dict[str, Any],
        ) -> ToolCallResult:
            self.calls.append(tool_call)
            return ToolCallResult(
                tool_name=REQUEST_RECORD_APPROVAL,
                status="success",
                response={
                    "input_required": True,
                    "approval_created": False,
                    "record_applied": False,
                    "action_name": (
                        CREATE_NUTRITION_MEAL_RECORD
                    ),
                    "missing_fields": ["foods[].portion"],
                    "input_request": {
                        "message_title": "섭취량 입력",
                        "text": "실제 섭취량을 입력해 주세요.",
                        "tables": None,
                        "selections": None,
                        "inputs": [
                            {
                                "type": "number",
                                "label": "1. 토스트 섭취량",
                                "value": None,
                                "options": {
                                    "unit": "g",
                                    "lower": 1,
                                    "upper": 5000,
                                    "selections": None,
                                },
                            }
                        ],
                    },
                },
                idempotency_key=(
                    f"{trace_id}:input-required"
                ),
            )

    provider = NativeFakeProvider()
    executor = InputRequiredExecutor()
    graph_runner = ToolChatAgentGraph(
        provider=provider,
        tool_runtime=ToolRuntime(executor),
        agent_name="nutrition_management_agent",
        prompt=nutrition_management_agent_prompt(),
        response_mode="nutrition_management_chat",
        decision_type="tool_call",
        tool_names=(REQUEST_RECORD_APPROVAL,),
        source_event_type=(
            SOURCE_NUTRITION_MANAGEMENT_AGENT
        ),
    )

    response = asyncio.run(
        graph_runner.continue_with_tool_calls(
            "trace-food-input-required",
            {
                "patient_id": "demo-patient",
                "message": "토스트를 기록해줘",
                "context": {},
            },
            tool_calls=[
                {
                    "id": "trusted-food-approval",
                    "name": REQUEST_RECORD_APPROVAL,
                    "arguments": {
                        "action_name": (
                            CREATE_NUTRITION_MEAL_RECORD
                        ),
                        "record_arguments": {
                            "meal_type": "breakfast",
                            "foods": [
                                {
                                    "food_name": "토스트",
                                    "portion": "100g",
                                }
                            ],
                        },
                    },
                }
            ],
        )
    )

    assert len(executor.calls) == 1
    assert provider.seen_payloads == []
    assert response.decision_type == "input_required"
    assert response.structured_payload[
        "input_required"
    ] is True
    chat_response = response.structured_payload[
        "chat_response"
    ]
    assert chat_response["message_type"] == "input_box"
    assert chat_response["message"]["inputs"][0][
        "label"
    ] == "1. 토스트 섭취량"


def test_multiturn_structured_delegation_does_not_require_final_text(monkeypatch):
    provider = BlankToolFinalizingProvider()
    monkeypatch.setattr(
        native_agent_main,
        "orchestrator",
        AgentLangGraphNativeOrchestrator(
            provider=provider,
            tool_executor=NativeFakeToolExecutor(),
        ),
    )
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="삶은 계란 정보를 확인해줘",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )

    response = invoke_multiturn_graph(request)

    assert response.human_summary == "삶은 계란 후보를 확인했습니다."
    assert response.structured_payload["food_candidates"]
    assert (
        response.structured_payload["final_answer_source"]
        == "specialist_handoff"
    )


def test_agent_app_multiturn_delegates_side_effect_continuation_to_medication_agent(monkeypatch):
    provider = NativeSideEffectLookupProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))

    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="I feel nauseous. Could it be the medication?",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )
    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    structured = payload["structured_payload"]
    assert payload["decision_type"] == "continuation_required"
    assert payload["human_summary"] == ""
    assert structured["routing_mode"] == "delegated_agent"
    assert structured["continuation_required"] is True
    assert structured["continuation_type"] == "side_effect_assessment"
    assert structured["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert [call["name"] for call in structured["specialist_tool_calls"]] == ["get_medication_side_effect_assessment"]
    assert tool_executor.calls == []
    supervisor_tools = set(provider.bound_tool_history[0])
    specialist_tools = set(provider.bound_tool_history[1])
    assert "delegate_to_medication_agent" in supervisor_tools
    assert {"get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"}.isdisjoint(supervisor_tools)
    assert "get_medication_side_effect_assessment" in specialist_tools
    assert "get_pro_ctcae_questionnaire" not in specialist_tools

    continuation = request.model_copy(deep=True)
    continuation.context = {
        "execute_tool_continuation": True,
        "continuation_tool_calls": structured["tool_calls"],
    }
    continuation_payload = invoke_multiturn_graph(continuation).model_dump(mode="json")
    continuation_structured = continuation_payload["structured_payload"]
    assert continuation_payload["decision_type"] == "side_effect_assessment"
    assert continuation_structured["routing_mode"] == "delegated_agent"
    assert continuation_structured["delegation_reason"] == "medication_tool_continuation"
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
    request = MultiturnChatRequest(
        patient_id="demo-patient",
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

    payload = invoke_multiturn_graph(request).model_dump(mode="json")
    assert payload["agent_name"] == "multiturn_chat_agent"
    assert payload["decision_type"] == "system_guidance"
    assert (
        payload["structured_payload"]["final_answer_source"]
        == "agent_loop_final_text"
    )
    assert "요청을 확인했습니다" not in payload["human_summary"]
    assert "메스꺼" in payload["human_summary"]
    assert "1번 자주 있다" in payload["human_summary"]
    assert provider.seen_payloads[0]["context"]["recent_chat"][1]["content"] == "속이 메스꺼운데 약때문일까?"
    assert "answer ordinary follow-up" in provider.seen_prompts[0]


def test_deterministic_provider_is_test_only_for_runtime_selection(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deterministic_test")
    monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="restricted"):
            create_llm_provider()
    finally:
        get_settings.cache_clear()


def test_deterministic_provider_is_available_only_in_explicit_testbed(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "deterministic_test")
    monkeypatch.setenv("APP_ENV", "testbed")
    get_settings.cache_clear()
    try:
        assert isinstance(create_llm_provider(), DeterministicTestProvider)
    finally:
        get_settings.cache_clear()


def test_ae_tool_calls_from_lookup_preserve_distinct_patient_symptoms():
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
            "assessments": [
                {
                    "match_status": "MATCHED",
                    "suspected": True,
                    "symptom_text": "속이 메스꺼워요",
                    "matched_concept": {"concept_id": "nausea"},
                },
                {
                    "match_status": "MATCHED",
                    "suspected": True,
                    "symptom_text": "실제로 두 번 토했어요",
                    "matched_concept": {"concept_id": "vomiting"},
                },
            ],
        },
    )
    tool_calls = ae_tool_calls_from_lookup(result)

    assert [call["name"] for call in tool_calls] == [
        "get_pro_ctcae_questionnaire",
        "get_pro_ctcae_questionnaire",
    ]
    assert [call["arguments"]["symptom_text"] for call in tool_calls] == [
        "속이 메스꺼워요",
        "실제로 두 번 토했어요",
    ]


def test_ambiguous_symptom_blocks_forced_pro_ctcae_questionnaire():
    result = ToolCallResult(
        tool_name="get_medication_side_effect_assessment",
        status="success",
        response={
            "suspected": True,
            "requires_clarification": True,
            "assessments": [
                {
                    "match_status": "AMBIGUOUS",
                    "suspected": False,
                    "symptom_text": "붉은 반점",
                    "candidate_concepts": [
                        {"concept_id": "rash"},
                        {"concept_id": "hives"},
                    ],
                }
            ],
        },
    )

    assert positive_side_effect_lookup(result) is False
    assert ae_tool_calls_from_lookup(result) == []


def test_deterministic_provider_requires_repeated_daily_pattern_before_policy_tool_call():
    provider = DeterministicTestProvider()

    daily_output = provider.model_output(
        {**build_daily_pattern().model_dump(mode="json"), "response_mode": "daily_pattern_analysis"}
    )
    repeated_daily_output = provider.model_output(
        {**build_repeated_daily_pattern().model_dump(mode="json"), "response_mode": "daily_pattern_analysis"}
    )
    chat_output = provider.model_output(
        build_taken_chat_request().model_dump(mode="json") | {"response_mode": "multiturn_chat"}
    )
    side_effect_output = provider.model_output(
        {
            **build_missed_payload().model_dump(mode="json"),
            "response_mode": "missed_dose_coaching",
            "chat_context": [{"role": "user", "content": "속이 메스꺼운데 약 때문일까?"}],
        }
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


def test_deterministic_provider_returns_general_chat_when_no_tool_needed():
    provider = DeterministicTestProvider()
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

    chat_output = provider.model_output(request.model_dump(mode="json") | {"response_mode": "multiturn_chat"})

    assert "tool_call" not in chat_output
    assert "속이 메스꺼운데 약때문일까?" in chat_output["message"]
    assert "1번 문항: 자주 있다" in chat_output["message"]


def test_deterministic_provider_answers_nutrition_and_medication_chat_together():
    provider = DeterministicTestProvider()
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="오늘 점심이 짰고 당 수치가 걱정돼요. 복약과 저녁 식사를 같이 조정해줘.",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"nutrition": {"today_summary": {"status": "exceeded"}}},
    )

    chat_output = provider.model_output(request.model_dump(mode="json") | {"response_mode": "multiturn_chat"})

    assert "tool_call" not in chat_output
    assert "tool_calls" not in chat_output
    assert "영양과 복약" in chat_output["message"]
    assert "저녁" in chat_output["message"]
    assert "처방된 복약 시간" in chat_output["message"]


def test_notification_policy_deltas_fill_daily_pattern_source():
    policies = notification_policy_deltas(
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
    policies = notification_policy_deltas(
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
