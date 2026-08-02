from __future__ import annotations

from typing import Any

import pytest

from agent_app.agents.multiturn_chat import SUPERVISOR_DIRECT_TOOLS
from agent_app.llm.prompts import multiturn_chat_prompt
from agent_app.tools.mcp_server import AgentMcpToolServer
from shared.schemas import ToolCallResult
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    PROPOSE_NOTIFICATION_POLICY,
    REQUEST_RECORD_APPROVAL,
    SOURCE_DAILY_PATTERN,
    SOURCE_MULTITURN_CHAT,
)
from shared.tool_permissions import allowed_tool_names_for_source

PATIENT_ID = "patient_0000000000000821"
POLICY_ID = "npol_0000000000000821"


class FakePolicyQueries:
    def __init__(self, policies: list[dict[str, Any]]) -> None:
        self.policies = policies
        self.calls: list[dict[str, Any]] = []

    def notification_policies(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(arguments)
        return {
            "success": True,
            "policies": list(self.policies),
            "total": len(self.policies),
            "source": "backend_read_db",
        }


def _approval_arguments(policy_id: str = POLICY_ID) -> dict[str, Any]:
    return {
        "action_name": CHANGE_NOTIFICATION_POLICY,
        "record_arguments": {
            "policy_id": policy_id,
            "decision": "apply",
            "changes": {"missed_dose_after_minutes": 120},
        },
    }


def _payload() -> dict[str, Any]:
    return {
        "patient_id": PATIENT_ID,
        "current_time": "2026-04-20T09:30:00+09:00",
        "context": {},
    }


def test_multiturn_chat_cannot_propose_notification_policy_creation() -> None:
    multiturn_tools = allowed_tool_names_for_source(
        SOURCE_MULTITURN_CHAT
    )

    assert PROPOSE_NOTIFICATION_POLICY not in multiturn_tools
    assert PROPOSE_NOTIFICATION_POLICY not in SUPERVISOR_DIRECT_TOOLS
    assert PROPOSE_NOTIFICATION_POLICY in allowed_tool_names_for_source(
        SOURCE_DAILY_PATTERN
    )
    prompt = multiturn_chat_prompt()
    assert "Notification policy creation is not supported in chat" in prompt
    assert "Never offer to create a policy" in prompt


@pytest.mark.asyncio
async def test_runtime_denies_notification_policy_proposal_from_chat() -> None:
    server = object.__new__(AgentMcpToolServer)

    result = await server._execute_tool_result(
        PROPOSE_NOTIFICATION_POLICY,
        {
            "slot_label": "아침",
            "extra_reminders": 1,
            "interval_minutes": 10,
            "effective_start_date": "2026-04-20",
            "effective_end_date": "2026-04-27",
            "reason": "사용자 요청",
            "source": "patient_request",
        },
        trace_id="trace-policy-create-denied",
        source_event_type=SOURCE_MULTITURN_CHAT,
        payload=_payload(),
        tool_call_id="propose-policy-from-chat",
    )

    assert result.status == "error"
    assert result.error == "tool_permission_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policies", "reason_code"),
    [
        ([], "ACTIVE_NOTIFICATION_POLICY_NOT_FOUND"),
        (
            [
                {
                    "policy_id": POLICY_ID,
                    "slot_label": "아침",
                    "active": False,
                    "version": 1,
                }
            ],
            "ACTIVE_NOTIFICATION_POLICY_NOT_FOUND",
        ),
    ],
)
async def test_policy_approval_fails_closed_without_exact_active_target(
    policies: list[dict[str, Any]],
    reason_code: str,
) -> None:
    server = object.__new__(AgentMcpToolServer)
    queries = FakePolicyQueries(policies)
    server.backend_queries = queries

    result = await server._execute_tool_result(
        REQUEST_RECORD_APPROVAL,
        _approval_arguments(),
        trace_id="trace-policy-target-unavailable",
        source_event_type=SOURCE_MULTITURN_CHAT,
        payload=_payload(),
        tool_call_id="request-policy-approval",
    )

    assert result.status == "success"
    assert result.response["policy_target_unavailable"] is True
    assert result.response["approval_created"] is False
    assert result.response["reason_code"] == reason_code
    assert queries.calls == [
        {
            "patient_id": PATIENT_ID,
            "policy_id": POLICY_ID,
            "active_only": True,
        }
    ]


@pytest.mark.asyncio
async def test_policy_approval_uses_backend_verified_active_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = object.__new__(AgentMcpToolServer)
    queries = FakePolicyQueries(
        [
            {
                "policy_id": POLICY_ID,
                "slot_label": "아침",
                "active": True,
                "version": 4,
            }
        ]
    )
    server.backend_queries = queries
    captured: dict[str, Any] = {}

    async def prepare(
        tool_name: str,
        arguments: dict[str, Any],
        **kwargs: Any,
    ) -> ToolCallResult:
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
            idempotency_key="policy-approval",
        )

    monkeypatch.setattr(server, "_prepare_internal_approval", prepare)

    result = await server._execute_tool_result(
        REQUEST_RECORD_APPROVAL,
        _approval_arguments(),
        trace_id="trace-policy-target-resolved",
        source_event_type=SOURCE_MULTITURN_CHAT,
        payload=_payload(),
        tool_call_id="request-policy-approval",
    )

    assert result.status == "confirmation_required"
    assert captured == {
        "tool_name": CHANGE_NOTIFICATION_POLICY,
        "arguments": {
            "policy_id": POLICY_ID,
            "decision": "apply",
            "changes": {"missed_dose_after_minutes": 120},
        },
        "result_tool_name": REQUEST_RECORD_APPROVAL,
    }


@pytest.mark.asyncio
async def test_policy_lookup_declares_modify_only_contract() -> None:
    server = object.__new__(AgentMcpToolServer)
    server.backend_queries = FakePolicyQueries([])

    result = await server._get_notification_policies(
        {"slot_label": "아침", "active_only": True},
        trace_id="trace-policy-lookup",
        payload=_payload(),
    )

    assert result.status == "success"
    assert result.response["operation_constraints"] == {
        "creation_supported": False,
        "change_requires_active_policy": True,
        "active_target_status": "not_found",
    }
