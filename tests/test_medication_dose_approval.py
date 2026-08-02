from __future__ import annotations

from datetime import UTC, datetime

import pytest

from agent_app.tools.mcp_server import AgentMcpToolServer
from shared.schemas import ToolCallResult
from shared.tool_names import (
    REQUEST_RECORD_APPROVAL,
    SOURCE_MEDICATION_AGENT,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from shared.tool_permissions import validate_tool_permission

NOW = datetime(2026, 4, 20, 9, 30, tzinfo=UTC)


def _payload(events: list[dict], *, message: str) -> dict:
    return {
        "patient_id": "patient_0000000000000821",
        "message": message,
        "current_time": NOW,
        "context": {
            "request_metadata": {
                "request_id": "req_0000000000000821",
                "message_id": "user_msg_0000000000000821",
            },
            "trusted_patient_context": {
                "today_medication": {"dose_events": events}
            },
        },
    }


def _event(
    dose_event_id: str,
    medication_name: str,
    *,
    hour: int,
    slot_label: str,
) -> dict:
    return {
        "dose_event_id": dose_event_id,
        "medication_name": medication_name,
        "slot_label": slot_label,
        "scheduled_for": NOW.replace(hour=hour).isoformat(),
        "status": "scheduled",
    }


@pytest.mark.asyncio
async def test_approval_resolves_typo_to_trusted_event_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = object.__new__(AgentMcpToolServer)
    captured: dict = {}

    async def prepare(tool_name, arguments, **kwargs):
        captured.update(
            {
                "tool_name": tool_name,
                "arguments": arguments,
                "result_tool_name": kwargs["result_tool_name"],
            }
        )
        return ToolCallResult(
            tool_name=REQUEST_RECORD_APPROVAL,
            status="confirmation_required",
            response={"mutation_confirmation": {}},
            idempotency_key="dose-approval",
        )

    monkeypatch.setattr(
        server,
        "_prepare_internal_approval",
        prepare,
    )
    payload = _payload(
        [
            _event(
                "dose_metformin",
                "메트포르민 500mg",
                hour=8,
                slot_label="아침",
            ),
            _event(
                "dose_amlodipine",
                "암로디핀 5mg",
                hour=9,
                slot_label="아침",
            ),
        ],
        message="메트포르핀 복용했어 아침에",
    )

    result = await server._execute_tool_result(
        REQUEST_RECORD_APPROVAL,
        {
            "action_name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            "record_arguments": {
                "medication_name": "메트포르핀"
            },
        },
        trace_id="trace-dose-approval",
        source_event_type=SOURCE_MEDICATION_AGENT,
        payload=payload,
        tool_call_id="request-dose-approval-by-name",
    )

    assert result.status == "confirmation_required"
    assert captured == {
        "tool_name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "arguments": {"dose_event_id": "dose_metformin"},
        "result_tool_name": REQUEST_RECORD_APPROVAL,
    }


@pytest.mark.asyncio
async def test_ambiguous_target_returns_selection_before_approval() -> None:
    server = object.__new__(AgentMcpToolServer)
    payload = _payload(
        [
            _event(
                "dose_morning",
                "메트포르민 500mg",
                hour=8,
                slot_label="아침",
            ),
            _event(
                "dose_evening",
                "메트포르민 500mg",
                hour=18,
                slot_label="저녁",
            ),
        ],
        message="메트포르민 먹었어",
    )

    result = await server._execute_tool_result(
        REQUEST_RECORD_APPROVAL,
        {
            "action_name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            "record_arguments": {
                "medication_name": "메트포르민"
            },
        },
        trace_id="trace-dose-selection",
        source_event_type=SOURCE_MEDICATION_AGENT,
        payload=payload,
        tool_call_id="request-dose-approval-ambiguous",
    )

    assert result.status == "success"
    assert result.response["selection_required"] is True
    assert result.response["approval_created"] is False
    assert {
        item["dose_event_id"]
        for item in result.response["dose_selection"]["candidates"]
    } == {"dose_morning", "dose_evening"}


def test_exact_id_is_scoped_to_nested_trusted_snapshot() -> None:
    payload = _payload(
        [
            _event(
                "dose_metformin",
                "메트포르민 500mg",
                hour=8,
                slot_label="아침",
            )
        ],
        message="메트포르민 먹었어",
    )
    valid = {
        "name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "arguments": {"dose_event_id": "dose_metformin"},
    }
    invented = {
        "name": UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        "arguments": {"dose_event_id": "dose_invented"},
    }

    assert (
        validate_tool_permission(
            valid,
            source_event_type=SOURCE_MEDICATION_AGENT,
            payload=payload,
        )
        is None
    )
    assert "must match current chat context" in str(
        validate_tool_permission(
            invented,
            source_event_type=SOURCE_MEDICATION_AGENT,
            payload=payload,
        )
    )
