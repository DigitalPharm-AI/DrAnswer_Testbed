from __future__ import annotations

import asyncio
from typing import Any

from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.llm.prompts import medication_agent_prompt
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import ToolCallResult
from shared.tool_names import (
    REQUEST_RECORD_APPROVAL,
    SOURCE_MEDICATION_AGENT,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from tests.support.llm import NativeChatProvider


class NoSecondModelCallProvider(NativeChatProvider):
    def __init__(self) -> None:
        self.bound_tool_names: list[str] = []
        self.seen_payloads: list[dict[str, Any]] = []

    async def model_output(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
    ) -> dict[str, Any]:
        del system_prompt
        self.seen_payloads.append(user_payload)
        raise AssertionError("selection_must_stop_before_llm")


class SelectionRequiredExecutor:
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
        del source_event_type, payload
        self.calls.append(tool_call)
        candidates = [
            {
                "dose_event_id": "dose_morning",
                "medication_name": "메트포르민 500mg",
                "slot_label": "아침",
                "scheduled_for": "2026-04-20T08:00:00+09:00",
                "status": "scheduled",
                "selection_value": (
                    "메트포르민 500mg · 아침 08:00"
                ),
            },
            {
                "dose_event_id": "dose_evening",
                "medication_name": "메트포르민 500mg",
                "slot_label": "저녁",
                "scheduled_for": "2026-04-20T18:00:00+09:00",
                "status": "scheduled",
                "selection_value": (
                    "메트포르민 500mg · 저녁 18:00"
                ),
            },
        ]
        selections = [
            candidate["selection_value"]
            for candidate in candidates
        ]
        return ToolCallResult(
            tool_name=REQUEST_RECORD_APPROVAL,
            status="success",
            response={
                "selection_required": True,
                "approval_created": False,
                "record_applied": False,
                "selection_request": {
                    "message_title": "복약 항목 선택",
                    "text": "복용한 약을 선택해 주세요.",
                    "tables": None,
                    "selections": selections,
                    "inputs": None,
                },
                "dose_selection": {
                    "action_name": (
                        UPDATE_MEDICATION_DOSE_EVENT_STATUS
                    ),
                    "candidates": candidates,
                },
            },
            idempotency_key=(
                f"{trace_id}:dose-selection-required"
            ),
        )


def test_selection_result_stops_tool_loop_and_renders_card() -> None:
    provider = NoSecondModelCallProvider()
    executor = SelectionRequiredExecutor()
    graph = ToolChatAgentGraph(
        provider=provider,
        tool_runtime=ToolRuntime(executor),
        agent_name="medication_agent",
        prompt=medication_agent_prompt(),
        response_mode="medication_chat",
        decision_type="tool_call",
        tool_names=(REQUEST_RECORD_APPROVAL,),
        source_event_type=SOURCE_MEDICATION_AGENT,
    )

    response = asyncio.run(
        graph.continue_with_tool_calls(
            "trace-dose-selection-required",
            {
                "patient_id": "demo-patient",
                "message": "메트포르민 먹었어",
                "context": {},
            },
            tool_calls=[
                {
                    "id": "trusted-dose-approval",
                    "name": REQUEST_RECORD_APPROVAL,
                    "arguments": {
                        "action_name": (
                            UPDATE_MEDICATION_DOSE_EVENT_STATUS
                        ),
                        "record_arguments": {
                            "medication_name": "메트포르민"
                        },
                    },
                }
            ],
        )
    )

    assert len(executor.calls) == 1
    assert provider.seen_payloads == []
    assert response.decision_type == "selection_required"
    chat_response = response.structured_payload["chat_response"]
    assert chat_response["message_type"] == "selection_box"
    assert len(chat_response["message"]["selections"]) == 2
