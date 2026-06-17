from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import pytest

os.environ.setdefault("DA_DRUG_ENV_FILE", ".env,.env.agent_app")

from agent_app.graph import AgentLangGraphNativeOrchestrator  # noqa: E402
from agent_app.providers import BedrockAnthropicProvider  # noqa: E402
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
        self.calls.append(tool_call)
        tool_name = str(tool_call.get("name") or "")
        arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
        if tool_name == "mark_dose_taken":
            return ToolCallResult(
                tool_name="mark_dose_taken",
                status="success",
                response={
                    "dose_event_id": arguments.get("dose_event_id"),
                    "status": "taken",
                    "message": "테스트 복약 기록을 완료 처리했습니다.",
                },
                idempotency_key=f"{trace_id}:mark_dose_taken:{source_event_type}",
            )
        if tool_name == "lookup_side_effect_info":
            return ToolCallResult(
                tool_name="lookup_side_effect_info",
                status="success",
                response={
                    "suspected": True,
                    "matched_effects": ["메스꺼움"],
                    "matched_items": ["테스트 항암제"],
                    "severity": "moderate",
                    "evidence": "테스트 PHR 주의사항에 메스꺼움이 포함되어 있습니다.",
                    "recommendation": "PRO-CTCAE 문항 확인이 필요합니다.",
                },
                idempotency_key=f"{trace_id}:lookup_side_effect_info",
            )
        if tool_name == "AE_pro_ctcae":
            return ToolCallResult(
                tool_name="AE_pro_ctcae",
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
                idempotency_key=f"{trace_id}:AE_pro_ctcae",
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
async def test_bedrock_multiturn_mark_dose_taken_executes_tool() -> None:
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
    assert response.structured_payload["tools_executed"] is True
    assert [call["name"] for call in executor.calls] == ["mark_dose_taken"]
    assert response.structured_payload["tool_results"][0]["tool_name"] == "mark_dose_taken"
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

    assert response.decision_type == "side_effect_assessment"
    assert response.structured_payload["tools_executed"] is True
    assert [call["name"] for call in executor.calls] == ["lookup_side_effect_info", "AE_pro_ctcae"]
    assert [result["tool_name"] for result in response.structured_payload["tool_results"]] == ["lookup_side_effect_info", "AE_pro_ctcae"]
    assert response.structured_payload["side_effect_status"] == "suspected"
    assert response.structured_payload["ae_pro_ctcae"]["matched"] is True
