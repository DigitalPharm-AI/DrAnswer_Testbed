from __future__ import annotations

import ast
import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient

import agent_app.main as native_agent_main
from agent_app.async_tasks import DEAD, enqueue_async_task
from agent_app.graph import AgentLangGraphNativeOrchestrator
from agent_app.providers import BaseLLMProvider, RuleBasedProvider
from agent_app.response_builders import missed_dose_hybrid_payload
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_executor import McpAgentToolExecutor
from agent_app.tool_policy import _notification_policy_deltas
from agent_app.tool_protocol import (
    MCP_METHOD_TOOLS_CALL,
    MCP_METHOD_TOOLS_LIST,
    mcp_json_rpc_request,
    mcp_result_from_tool_result,
    mcp_tools_list,
    tool_result_from_mcp_result,
)
from agent_app.tool_side_effects import ae_tool_call_from_lookup
from shared.schemas import DailyMedicationPattern, DosePatternEvent, MissedDoseEventPayload, MultiturnChatRequest, SlotAdherenceSummary, ToolCallResult
from shared.settings import get_settings


class NativeFakeProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.seen_payloads: list[dict[str, Any]] = []
        self.seen_prompts: list[str] = []

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        self.seen_prompts.append(system_prompt)
        self.seen_payloads.append(user_payload)
        response_mode = user_payload.get("response_mode")
        if response_mode == "daily_pattern_analysis":
            return {
                "summary": "아침 시간대 미복용이 반복됩니다.",
                "tool_call": {
                    "name": "apply_notification_policy",
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
                    "name": "mark_dose_taken",
                    "arguments": {
                        "dose_event_id": 12,
                        "reason": "patient_reported_taken",
                    },
                },
            }
        return {"advice": "현재 복약 상태를 확인했습니다.", "observations": ["추가 도구 실행은 필요하지 않습니다."]}


class NativeSideEffectLookupProvider(BaseLLMProvider):
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert user_payload["response_mode"] == "multiturn_chat"
        return {
            "message": "복용약 주의사항을 먼저 확인하겠습니다.",
            "tool_call": {
                "name": "lookup_side_effect_info",
                "arguments": {
                    "symptom_text": "속이 메스꺼워요.",
                    "medication_name": "항암제",
                },
            },
        }


class NativeRecentChatProvider(BaseLLMProvider):
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


class InvalidMissedDoseHybridProvider(BaseLLMProvider):
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert user_payload["response_mode"] == "missed_dose_coaching"
        return {
            "patient_message": "확인했습니다.",
            "likely_reason": "unknown",
        }


class UnsafeMissedDoseToolProvider(BaseLLMProvider):
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        assert user_payload["response_mode"] == "missed_dose_coaching"
        return {
            "patient_message": "복용 완료를 기록하겠습니다.",
            "tool_call": {
                "name": "mark_dose_taken",
                "arguments": {"dose_event_id": 12},
            },
        }


class NativeFakeToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool_call(self, tool_call: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> ToolCallResult:
        self.calls.append(tool_call)
        name = str(tool_call.get("name"))
        if name == "mark_dose_taken":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "dose_event_id": tool_call["arguments"]["dose_event_id"],
                    "status": "taken",
                    "message": "아침 08:00 혈압약 복약을 완료로 기록했습니다.",
                },
                idempotency_key=f"{trace_id}:mark_dose_taken:{source_event_type}:12",
            )
        if name == "apply_notification_policy":
            return ToolCallResult(
                tool_name=name,
                status="success",
                response={
                    "idempotency_key": f"{trace_id}:apply_notification_policy:{source_event_type}",
                    "results": [{"slot_label": "아침 08:00", "applied": True, "message": "정책을 적용했습니다."}],
                    "all_applied": True,
                },
                idempotency_key=f"{trace_id}:apply_notification_policy:{source_event_type}",
            )
        if name == "lookup_side_effect_info":
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
                idempotency_key=f"{trace_id}:lookup_side_effect_info",
            )
        if name == "AE_pro_ctcae":
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
                idempotency_key=f"{trace_id}:AE_pro_ctcae",
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
    assert {tool["name"] for tool in payload["tools"]} >= {"mark_dose_taken", "lookup_side_effect_info", "AE_pro_ctcae"}
    for tool in payload["tools"]:
        assert tool["inputSchema"]["type"] == "object"
        assert "properties" in tool["inputSchema"]
        assert tool["outputSchema"]["type"] == "object"


def test_tool_result_round_trips_through_mcp_shape():
    result = ToolCallResult(
        tool_name="mark_dose_taken",
        status="success",
        response={"status": "taken", "message": "복용 완료로 기록했습니다."},
        idempotency_key="trace:mark_dose_taken",
    )

    mcp_result = mcp_result_from_tool_result(result)
    restored = tool_result_from_mcp_result("mark_dose_taken", mcp_result)

    assert mcp_result["isError"] is False
    assert mcp_result["content"][0]["type"] == "text"
    assert mcp_result["structuredContent"]["tool_name"] == "mark_dose_taken"
    assert restored == result


def test_mcp_agent_tool_executor_calls_tools_call_json_rpc():
    server = RecordingMcpServer()
    executor = McpAgentToolExecutor(server=server)

    result = asyncio.run(
        executor.execute_tool_call(
            {"name": "mark_dose_taken", "arguments": {"dose_event_id": 12}},
            trace_id="trace-mcp",
            source_event_type="multiturn_chat",
            payload={"patient_id": "demo-patient"},
        )
    )

    assert result.tool_name == "mark_dose_taken"
    assert result.status == "success"
    assert server.requests[0]["request"]["method"] == MCP_METHOD_TOOLS_CALL
    assert server.requests[0]["request"]["params"] == {"name": "mark_dose_taken", "arguments": {"dose_event_id": 12}}
    assert server.requests[0]["trace_id"] == "trace-mcp"
    assert server.requests[0]["source_event_type"] == "multiturn_chat"


def test_agent_app_mcp_tools_list_endpoint_reports_catalog():
    request = mcp_json_rpc_request(MCP_METHOD_TOOLS_LIST, request_id="tools-list")

    response = TestClient(native_agent_main.app).post("/agent/mcp", json=request, headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["jsonrpc"] == "2.0"
    assert payload["id"] == "tools-list"
    tool_names = {tool["name"] for tool in payload["result"]["tools"]}
    assert {"mark_dose_taken", "lookup_side_effect_info", "AE_pro_ctcae"}.issubset(tool_names)


def test_agent_app_mcp_direct_call_enforces_default_tool_allowlist():
    request = mcp_json_rpc_request(
        MCP_METHOD_TOOLS_CALL,
        {"name": "mark_dose_taken", "arguments": {"dose_event_id": 12}},
        request_id="blocked-tool",
    )

    response = TestClient(native_agent_main.app).post("/agent/mcp", json=request, headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    result = payload["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["tool_name"] == "mark_dose_taken"
    assert result["structuredContent"]["status"] == "error"
    assert result["structuredContent"]["error"] == "tool_permission_denied"
    assert result["structuredContent"]["response"]["source_event_type"] == "mcp"


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
    assert payload["structured_payload"]["tool_results"][0]["tool_name"] == "apply_notification_policy"
    assert payload["structured_payload"]["tool_results"][0]["status"] == "skipped"
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


def test_missed_dose_output_validator_falls_back_when_hybrid_required(monkeypatch):
    provider = InvalidMissedDoseHybridProvider()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=NativeFakeToolExecutor())
    payload = build_missed_payload().model_copy(
        update={
            "adherence_pattern_context": {"pattern_code": "B"},
            "tone_policy_context": {"tone_key": "persuasion"},
        }
    )

    response = asyncio.run(orchestrator.invoke("missed_dose", payload.model_dump(mode="json")))

    structured = response.structured_payload
    hybrid = structured["missed_dose_hybrid"]
    assert hybrid["generated_message"] == "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요."
    assert hybrid["reason"] == "LLM 출력 검증 실패로 안전 기본 문구를 사용했습니다."
    assert hybrid["pattern_code"] == "B"
    assert hybrid["tone_key"] == "persuasion"
    assert "validation_error" in structured["model_output"]


def test_missed_dose_tool_permission_blocks_mark_taken(monkeypatch):
    provider = UnsafeMissedDoseToolProvider()
    tool_executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor)

    response = asyncio.run(orchestrator.invoke("missed_dose", build_missed_payload().model_dump(mode="json")))

    structured = response.structured_payload
    assert structured["tool_call"]["name"] == "mark_dose_taken"
    assert structured["tool_results"][0]["tool_name"] == "mark_dose_taken"
    assert structured["tool_results"][0]["status"] == "error"
    assert structured["tool_results"][0]["error"] == "tool_permission_denied"
    assert "missed_dose" in structured["tool_results"][0]["response"]["source_event_type"]
    assert tool_executor.calls == []


def test_agent_app_multiturn_mark_taken_tool_call(monkeypatch):
    provider = NativeFakeProvider()
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=provider, tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)

    response = client.post("/agent/multiturn-chat", json=build_taken_chat_request().model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision_type"] == "tool_call"
    assert payload["structured_payload"]["tool_call"]["name"] == "mark_dose_taken"
    assert payload["structured_payload"]["tool_call"]["arguments"]["dose_event_id"] == 12
    assert payload["structured_payload"]["tools_executed"] is True
    assert payload["structured_payload"]["tool_results"][0]["tool_name"] == "mark_dose_taken"
    assert tool_executor.calls[0]["name"] == "mark_dose_taken"


def test_agent_app_multiturn_forces_ae_after_positive_side_effect_lookup(monkeypatch):
    tool_executor = NativeFakeToolExecutor()
    monkeypatch.setattr(native_agent_main, "orchestrator", AgentLangGraphNativeOrchestrator(provider=NativeSideEffectLookupProvider(), tool_executor=tool_executor))
    client = TestClient(native_agent_main.app)

    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-demo",
        event_type="multiturn_chat",
        message="속이 메스꺼운데 약 때문일까?",
        current_time=datetime(2026, 4, 20, 9, 35),
        context={"recent_chat": []},
    )
    response = client.post("/agent/multiturn-chat", json=request.model_dump(mode="json"), headers=internal_auth_headers())

    assert response.status_code == 200
    payload = response.json()
    assert payload["decision_type"] == "async_continuation_requested"
    assert payload["structured_payload"]["async_continuation_type"] == "side_effect_assessment"
    assert [call["name"] for call in payload["structured_payload"]["tool_calls"]] == ["lookup_side_effect_info"]
    assert tool_executor.calls == []

    continuation = request.model_copy(deep=True)
    continuation.context = {
        "execute_async_continuation": True,
        "async_tool_calls": payload["structured_payload"]["tool_calls"],
    }
    continuation_response = client.post("/agent/multiturn-chat", json=continuation.model_dump(mode="json"), headers=internal_auth_headers())

    assert continuation_response.status_code == 200
    continuation_payload = continuation_response.json()
    assert continuation_payload["decision_type"] == "side_effect_assessment"
    assert [call["name"] for call in continuation_payload["structured_payload"]["tool_calls"]] == ["lookup_side_effect_info", "AE_pro_ctcae"]
    assert [result["tool_name"] for result in continuation_payload["structured_payload"]["tool_results"]] == ["lookup_side_effect_info", "AE_pro_ctcae"]
    assert [call["name"] for call in tool_executor.calls] == ["lookup_side_effect_info", "AE_pro_ctcae"]
    assert tool_executor.calls[1]["arguments"]["symptom_normalize"] == "메스꺼움"
    assert "항암제 주의사항" in continuation_payload["human_summary"]
    assert "메스꺼움 관련 가능성" in continuation_payload["human_summary"]
    assert "PRO-CTCAE 자기보고 문항" in continuation_payload["human_summary"]


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
    assert "요청을 확인했습니다" not in payload["human_summary"]
    assert "메스꺼" in payload["human_summary"]
    assert "1번 자주 있다" in payload["human_summary"]
    assert provider.seen_payloads[0]["context"]["recent_chat"][1]["content"] == "속이 메스꺼운데 약때문일까?"
    assert "answer ordinary follow-up" in provider.seen_prompts[0]


def test_ae_tool_call_from_lookup_normalizes_generic_phr_effect_to_pro_ctcae_symptom():
    result = ToolCallResult(
        tool_name="lookup_side_effect_info",
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
            "name": "lookup_side_effect_info",
            "arguments": {"symptom_text": "속이 메스꺼운데 약때문일까?"},
        },
        result,
        {"message": "속이 메스꺼운데 약때문일까?"},
    )

    assert tool_call["name"] == "AE_pro_ctcae"
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
    assert repeated_daily_output["tool_calls"][0]["name"] == "apply_notification_policy"
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
