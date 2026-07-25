from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

_shared_env = Path(".env") if Path(".env").exists() else Path("..") / ".env"
os.environ.setdefault("DA_DRUG_ENV_FILE", f"{_shared_env},.env.agent_app")

from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator  # noqa: E402
from agent_app.providers.bedrock import BedrockAnthropicProvider  # noqa: E402
from shared.schemas import MultiturnChatRequest, ToolCallResult  # noqa: E402
from shared.settings import get_settings  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_BEDROCK_TOOLCALL_TESTS") != "1",
    reason="Set RUN_BEDROCK_TOOLCALL_TESTS=1 to run live Bedrock tool-calling tests.",
)


class RecordingToolExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute_tool_call(self, tool_call: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> ToolCallResult:
        self.calls.append({**tool_call, "_source_event_type": source_event_type})
        tool_name = str(tool_call.get("name") or "")
        arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
        if tool_name == "update_medication_dose_event_status":
            return ToolCallResult(
                tool_name="update_medication_dose_event_status",
                status="success",
                response={
                    "dose_event_id": arguments.get("dose_event_id"),
                    "status": "taken",
                    "message": "테스트 복약 기록을 완료 처리했습니다.",
                },
                idempotency_key=f"{trace_id}:update_medication_dose_event_status:{source_event_type}",
            )
        if tool_name == "get_medication_side_effect_assessment":
            return ToolCallResult(
                tool_name="get_medication_side_effect_assessment",
                status="success",
                response={
                    "suspected": True,
                    "matched_effects": ["메스꺼움"],
                    "matched_items": ["테스트 항암제"],
                    "severity": "moderate",
                    "evidence": "테스트 PHR 주의사항에 메스꺼움이 포함되어 있습니다.",
                    "recommendation": "PRO-CTCAE 문항 확인이 필요합니다.",
                },
                idempotency_key=f"{trace_id}:get_medication_side_effect_assessment",
            )
        if tool_name == "get_pro_ctcae_questionnaire":
            return ToolCallResult(
                tool_name="get_pro_ctcae_questionnaire",
                status="success",
                response={
                    "input_symptom": arguments.get("symptom_normalize") or arguments.get("symptom_text") or "메스꺼움",
                    "matched": True,
                    "match_type": "exact",
                    "matched_symptom_term": "Nausea",
                    "matched_korean_symptom_name": "메스꺼움",
                    "similarity": 1.0,
                    "threshold": 0.56,
                    "scoring_method": "live_test_fake_tool",
                    "embedding_provider": "",
                    "sheet_name": "Parsed_Items",
                    "questions": [],
                    "candidates": [],
                },
                idempotency_key=f"{trace_id}:get_pro_ctcae_questionnaire",
            )
        return ToolCallResult(tool_name=tool_name or "unknown", status="error", error=f"unexpected_tool:{tool_name}")


def _build_orchestrator() -> tuple[AgentLangGraphNativeOrchestrator, RecordingToolExecutor]:
    get_settings.cache_clear()
    settings = get_settings()
    if not (settings.aws_bearer_token_bedrock or settings.aws_profile or (settings.aws_access_key_id and settings.aws_secret_access_key)):
        pytest.skip("Bedrock credentials are not configured.")
    executor = RecordingToolExecutor()
    return AgentLangGraphNativeOrchestrator(provider=BedrockAnthropicProvider(), tool_executor=executor), executor


@pytest.mark.asyncio
async def test_bedrock_multiturn_delegates_dose_update_to_medication_agent() -> None:
    orchestrator, executor = _build_orchestrator()
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        event_type="multiturn_chat",
        message="아침 혈압약은 방금 복용했어. 복용 완료로 기록해줘.",
        current_time=datetime(2026, 5, 19, 9, 35),
        context={
            "today_dose_events": [
                {
                    "dose_event_id": 12,
                    "medication_name": "혈압약",
                    "slot_label": "아침 08:00",
                    "scheduled_for": "2026-05-19T08:00:00",
                    "status": "missed",
                }
            ],
            "schedule_slots": ["아침 08:00"],
        },
    )

    response = await orchestrator.invoke("multiturn_chat", request.model_dump(mode="json"))

    assert response.decision_type == "tool_call"
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert response.structured_payload["tools_executed"] is True
    assert [call["name"] for call in executor.calls] == ["update_medication_dose_event_status"]
    assert executor.calls[0]["_source_event_type"] == "medication_agent"
    assert response.structured_payload["tool_results"][0]["tool_name"] == "update_medication_dose_event_status"
    assert response.structured_payload["tool_results"][0]["response"]["status"] == "taken"


@pytest.mark.asyncio
async def test_bedrock_multiturn_side_effect_lookup_forces_ae_pro_ctcae() -> None:
    orchestrator, executor = _build_orchestrator()
    request = MultiturnChatRequest(
        patient_id="demo-patient",
        phr_patient_key="phr-live-tool-test",
        event_type="multiturn_chat",
        message="항암제 복용 후 속이 메스꺼운데 약 때문일까요? 복용약 주의사항을 먼저 확인해줘.",
        current_time=datetime(2026, 5, 19, 9, 40),
        context={
            "medication_name": "테스트 항암제",
            "recent_chat": [],
            "today_dose_events": [],
            "schedule_slots": ["아침 08:00"],
        },
    )

    response = await orchestrator.invoke("multiturn_chat", request.model_dump(mode="json"))

    assert response.decision_type == "async_continuation_requested"
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert response.structured_payload["async_continuation_required"] is True
    assert response.structured_payload["async_continuation_type"] == "side_effect_assessment"
    assert [call["name"] for call in response.structured_payload["tool_calls"]] == ["get_medication_side_effect_assessment"]
    assert executor.calls == []

    continuation = request.model_copy(deep=True)
    continuation.context = {
        **request.context,
        "execute_async_continuation": True,
        "async_tool_calls": response.structured_payload["tool_calls"],
    }
    response = await orchestrator.invoke("multiturn_chat", continuation.model_dump(mode="json"))

    assert response.decision_type == "side_effect_assessment"
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["supervisor_tool_calls"][0]["name"] == "delegate_to_medication_agent"
    assert response.structured_payload["tools_executed"] is True
    assert [call["name"] for call in executor.calls] == ["get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"]
    assert all(call["_source_event_type"] == "medication_agent" for call in executor.calls)
    assert [result["tool_name"] for result in response.structured_payload["tool_results"]] == ["get_medication_side_effect_assessment", "get_pro_ctcae_questionnaire"]
    assert response.structured_payload["side_effect_status"] == "suspected"
    assert response.structured_payload["ae_pro_ctcae"]["matched"] is True
