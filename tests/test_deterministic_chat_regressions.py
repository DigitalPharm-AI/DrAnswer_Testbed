from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest

from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.providers.deterministic_test import DeterministicTestProvider
from shared.chat_contracts import ChatSyncRequest, agent_chat_payload
from shared.schemas import MissedDoseEventPayload, ToolCallResult
from shared.tool_names import (
    DELEGATE_TO_MEDICATION_AGENT,
    GET_MEDICATION_DOSE_STATUS,
    SOURCE_MEDICATION_AGENT,
)
from shared.tool_permissions import validate_tool_permission


class DoseStatusExecutor:
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
        assert validate_tool_permission(
            tool_call,
            source_event_type=source_event_type,
            payload=payload,
        ) is None
        self.calls.append({**tool_call, "_source_event_type": source_event_type})
        assert tool_call["name"] == GET_MEDICATION_DOSE_STATUS
        target_date = str(tool_call["arguments"]["target_date"])
        return ToolCallResult(
            tool_name=GET_MEDICATION_DOSE_STATUS,
            status="success",
            response={
                "success": True,
                "patient_id": payload["patient_id"],
                "target_date": target_date,
                "start_date": target_date,
                "end_date": target_date,
                "dose_events": [
                    {
                        "dose_event_id": "dose-1",
                        "medication_name": "예시약",
                        "slot_label": "아침",
                        "scheduled_for": f"{target_date}T08:00:00+09:00",
                        "status": "taken",
                    },
                    {
                        "dose_event_id": "dose-2",
                        "medication_name": "예시약",
                        "slot_label": "저녁",
                        "scheduled_for": f"{target_date}T18:00:00+09:00",
                        "status": "scheduled",
                    },
                ],
                "total": 2,
                "summary_by_date": [],
                "totals_by_status": {"taken": 1, "scheduled": 1, "missed": 0},
            },
            idempotency_key=f"{trace_id}:{GET_MEDICATION_DOSE_STATUS}",
        )


def _sync_request(message: str) -> ChatSyncRequest:
    return ChatSyncRequest(
        request_id="req_0000000000000001",
        message_id="user_msg_0000000000000001",
        patient_id="patient_0000000000000001",
        requested_return_type="text",
        message=message,
        message_at=datetime(2026, 7, 25, 10, 30, tzinfo=UTC),
    )


def test_deterministic_missed_dose_with_context_includes_valid_safe_hybrid() -> None:
    payload = MissedDoseEventPayload(
        patient_id="patient_0000000000000001",
        dose_event_id=11,
        medication_name="예시약",
        slot_label="아침",
        scheduled_for=datetime(2026, 7, 25, 8, 0),
        detected_at=datetime(2026, 7, 25, 9, 0),
        adherence_pattern_context={
            "pattern_code": "B",
            "reason": "복약 루틴 형성을 위한 단기 미복용 확인이 필요합니다.",
        },
        tone_policy_context={
            "pattern_code": "B",
            "tone_key": "persuasion",
            "message": "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
            "message_variant": "v1",
            "message_catalog_source": "test_catalog",
        },
    ).model_dump(mode="json")

    response = asyncio.run(
        AgentLangGraphNativeOrchestrator(DeterministicTestProvider()).invoke(
            "missed_dose",
            payload,
            trace_id="trace-deterministic-missed-dose",
        )
    )

    hybrid = response.structured_payload["missed_dose_hybrid"]
    assert response.decision_type == "missed_dose_assessment"
    assert response.human_summary == hybrid["generated_message"]
    assert len(hybrid["generated_message"]) <= 45
    assert hybrid["safety_notes"] == [
        "llm_generated",
        "no_medication_name",
        "no_diagnosis",
        "non_directive",
        "schema_validated",
    ]


def test_deterministic_reads_recent_chat_from_backend_context_boundary() -> None:
    request = _sync_request("아까 무슨 말 했었지?")
    payload = agent_chat_payload(
        request,
        backend_context={
            "backend_message_verified": True,
            "recent_chat": [
                {"role": "user", "content": "오늘 아침 약은 복용했어요."},
                {"role": "assistant", "content": "복용 기록을 확인했습니다."},
            ],
        },
    )

    output = DeterministicTestProvider().model_output(
        {
            **payload,
            "response_mode": "multiturn_chat",
        }
    )

    assert "오늘 아침 약은 복용했어요." in output["message"]
    assert "복용 기록을 확인했습니다." in output["message"]


@pytest.mark.parametrize(
    "message",
    [
        "오늘 약은 언제 먹어야 해?",
        "오늘 남은 약 알려줘",
        "오늘 복용 상태를 확인해줘",
    ],
)
def test_deterministic_clear_medication_queries_delegate_to_medication_agent(message: str) -> None:
    payload = agent_chat_payload(_sync_request(message))

    supervisor_output = DeterministicTestProvider().model_output(
        {
            **payload,
            "response_mode": "multiturn_chat",
        }
    )
    specialist_output = DeterministicTestProvider().model_output(
        {
            **payload,
            "response_mode": "medication_chat",
        }
    )

    assert supervisor_output["tool_calls"][0]["name"] == DELEGATE_TO_MEDICATION_AGENT
    assert specialist_output["tool_calls"] == [
        {
            "name": GET_MEDICATION_DOSE_STATUS,
            "arguments": {"target_date": "2026-07-25"},
        }
    ]


def test_deterministic_medication_query_uses_existing_specialist_tool_flow() -> None:
    executor = DoseStatusExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(
        DeterministicTestProvider(),
        tool_executor=executor,
    )
    payload = agent_chat_payload(_sync_request("오늘 남은 약과 복용 상태 알려줘"))

    response = asyncio.run(
        orchestrator.invoke(
            "multiturn_chat",
            payload,
            trace_id="trace-deterministic-dose-status",
        )
    )

    assert len(executor.calls) == 1
    assert executor.calls[0]["name"] == GET_MEDICATION_DOSE_STATUS
    assert executor.calls[0]["arguments"] == {"target_date": "2026-07-25"}
    assert executor.calls[0]["_source_event_type"] == SOURCE_MEDICATION_AGENT
    assert response.structured_payload["routing_mode"] == "delegated_agent"
    assert response.structured_payload["specialist_agent"] == "medication_agent"
    assert response.structured_payload["tool_calls"][0]["name"] == GET_MEDICATION_DOSE_STATUS
    assert response.human_summary == "2026-07-25 복약 일정은 총 2건이고, 완료 1건, 미복용 0건, 예정 1건입니다."


def test_deterministic_ambiguous_request_asks_for_supported_scope() -> None:
    output = DeterministicTestProvider().model_output(
        {
            **agent_chat_payload(_sync_request("도와줘")),
            "response_mode": "multiturn_chat",
        }
    )

    assert "조금 더 구체적으로" in output["message"]
    assert "복약 일정·복용 여부 조회" in output["message"]
    assert "식사 기록" in output["message"]
