from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import delete

from agent_app.integration.contracts import (
    NotificationPolicyChangeResponse,
    NotificationPolicyChangeResult,
    RecordChangeResponse,
    RecordChangeResult,
)
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import AgentBackendWriteRequest
from agent_app.tools.backend_write import BACKEND_WRITE_TOOL_SPECS
from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.mcp_server import AgentMcpToolServer
from agent_app.tools.names import (
    BACKEND_V12_SYNC_WRITE_TOOLS,
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    GET_NOTIFICATION_POLICIES,
    UPDATE_NUTRITION_MEAL_RECORD,
)
from agent_app.tools.protocol import tool_result_from_mcp_result

NOW = datetime(2026, 7, 25, 16, 30, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clear_backend_write_state() -> None:
    with SessionLocal() as session:
        session.execute(delete(AgentBackendWriteRequest))
        session.commit()
    yield
    with SessionLocal() as session:
        session.execute(delete(AgentBackendWriteRequest))
        session.commit()


class FakeBackendV12Client:
    def __init__(self) -> None:
        self.record_requests = []
        self.policy_requests = []

    async def change_record(self, request):
        self.record_requests.append(request)
        return RecordChangeResponse(
            success=True,
            request_id=request.request_id,
            result=RecordChangeResult(
                resource_type=request.resource_type,
                operation=request.operation,
                record_id=request.record_id or "meal-101",
                parent_record_id=request.parent_record_id,
                version=1 if request.operation == "create" else (request.expected_version or 0) + 1,
            ),
            error=None,
            processed_at=NOW,
        )

    async def change_notification_policy(self, request):
        self.policy_requests.append(request)
        return NotificationPolicyChangeResponse(
            success=True,
            request_id=request.request_id,
            result=NotificationPolicyChangeResult(
                policy_id=request.policy_id,
                decision=request.payload.decision,
                applied=request.payload.decision == "apply",
                version=request.expected_version + (1 if request.payload.decision == "apply" else 0),
            ),
            error=None,
            processed_at=NOW,
        )


class FakeBackendQueryTools:
    def __init__(self) -> None:
        self.version_queries = []

    def record_version(
        self,
        *,
        patient_id,
        resource_type,
        record_id,
        parent_record_id=None,
    ):
        self.version_queries.append(
            {
                "patient_id": patient_id,
                "resource_type": resource_type,
                "record_id": record_id,
                "parent_record_id": parent_record_id,
            }
        )
        return 7

    def notification_policy_version(self, *, patient_id, policy_id):
        self.version_queries.append(
            {
                "patient_id": patient_id,
                "resource_type": "notification_policy",
                "record_id": policy_id,
            }
        )
        return 4

    def notification_policies(self, *, patient_id, policy_id=None, slot_label=None, active_only=True):
        return {
            "success": True,
            "policies": [
                {
                    "policy_id": policy_id or "npol_0123456789abcdef",
                    "slot_label": slot_label or "아침",
                    "version": 4,
                }
            ],
            "total": 1,
            "source": "backend_read_db",
        }


def v12_payload() -> dict:
    return {
        "patient_id": "patient-001",
        "current_time": NOW,
        "context": {
            "request_metadata": {
                "contract_version": "v1.2",
                "request_id": "chat-request-001",
                "message_id": "501",
                "conversation_id": "conversation-001",
            }
        },
    }


async def execute(
    server: AgentMcpToolServer,
    *,
    name: str,
    arguments: dict,
    source_event_type: str,
    tool_call_id: str,
):
    result = await server.tools_call(
        {
            "name": name,
            "arguments": arguments,
            "tool_call_id": tool_call_id,
        },
        trace_id="internal-trace-not-sent",
        source_event_type=source_event_type,
        payload=v12_payload(),
    )
    return tool_result_from_mcp_result(name, result)


def test_backend_v12_write_registry_has_only_contract_supported_tools() -> None:
    assert frozenset(BACKEND_WRITE_TOOL_SPECS) == BACKEND_V12_SYNC_WRITE_TOOLS
    assert len(BACKEND_WRITE_TOOL_SPECS) == 7


def test_write_tool_catalog_exposes_only_business_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    create_tool = tools[CREATE_NUTRITION_MEAL_RECORD]
    update_tool = tools[UPDATE_NUTRITION_MEAL_RECORD]
    policy_tool = tools[CHANGE_NOTIFICATION_POLICY]

    assert create_tool["execution_mode"] == "backend_sync"
    assert create_tool["backend_endpoint"] == "/agent/sync/record-change"
    assert "expected_version" not in create_tool["required_arguments"]
    assert "expected_version" not in update_tool["required_arguments"]
    assert "patient_id" not in update_tool["optional_arguments"]
    assert "reason" not in update_tool["optional_arguments"]
    assert update_tool["inputSchema"]["additionalProperties"] is False
    assert policy_tool["backend_endpoint"] == "/agent/sync/notification-policy-change"
    assert policy_tool["annotations"]["idempotentHint"] is True
    assert policy_tool["required_arguments"] == ["policy_id", "decision"]
    assert policy_tool["inputSchema"]["additionalProperties"] is False
    assert policy_tool["inputSchema"]["properties"]["changes"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_v12_record_write_tool_calls_backend_in_same_turn_with_stable_request_id() -> None:
    client = FakeBackendV12Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )
    arguments = {
        "meal_type": "lunch",
        "meal_date": "2026-07-25",
        "foods": [
            {
                "food_name": "닭가슴살",
                "portion": "100g",
                "nutrients": {"calories": 165},
            }
        ],
    }

    first = await execute(
        server,
        name=CREATE_NUTRITION_MEAL_RECORD,
        arguments=arguments,
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-001",
    )
    second = await execute(
        server,
        name=CREATE_NUTRITION_MEAL_RECORD,
        arguments=arguments,
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-001",
    )

    assert first.status == second.status == "success"
    assert len(client.record_requests) == 1
    assert client.record_requests[0].source_chat_request_id == "chat-request-001"
    assert client.record_requests[0].confirmation_message_id == "501"
    assert client.record_requests[0].conversation_id == "conversation-001"
    assert client.record_requests[0].patient_id == "patient-001"
    assert "internal-trace-not-sent" not in client.record_requests[0].model_dump_json()


@pytest.mark.asyncio
async def test_v12_policy_write_tool_calls_dedicated_backend_endpoint_contract() -> None:
    client = FakeBackendV12Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)

    result = await execute(
        server,
        name=CHANGE_NOTIFICATION_POLICY,
        arguments={
            "policy_id": "npol_0123456789abcdef",
            "decision": "apply",
            "changes": {
                "extra_reminders": 2,
                "interval_minutes": 15,
            },
        },
        source_event_type="multiturn_chat",
        tool_call_id="tool-call-policy-001",
    )

    assert result.status == "success"
    assert len(client.policy_requests) == 1
    request = client.policy_requests[0]
    assert request.policy_id == "npol_0123456789abcdef"
    assert request.expected_version == 4
    assert request.payload.decision == "apply"
    assert request.payload.changes is not None
    assert request.payload.changes.interval_minutes == 15
    assert request.payload.reason == "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
    assert queries.version_queries[0]["record_id"] == "npol_0123456789abcdef"
    assert result.idempotency_key == request.request_id


@pytest.mark.asyncio
async def test_v12_update_write_tool_resolves_expected_version_inside_tool() -> None:
    client = FakeBackendV12Client()
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(backend_client=client, backend_queries=queries)

    result = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_id": 10,
            "description": "점심 기록 정정",
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-update-001",
    )
    replay = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_id": 10,
            "description": "점심 기록 정정",
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-update-001",
    )

    assert result.status == replay.status == "success"
    assert len(client.record_requests) == 1
    assert client.record_requests[0].expected_version == 7
    assert client.record_requests[0].patient_id == "patient-001"
    assert client.record_requests[0].payload.reason == "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행"
    assert queries.version_queries == [
        {
            "patient_id": "patient-001",
            "resource_type": "nutrition_meal",
            "record_id": 10,
            "parent_record_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_v12_write_tool_rejects_model_supplied_technical_arguments() -> None:
    client = FakeBackendV12Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )

    result = await execute(
        server,
        name=UPDATE_NUTRITION_MEAL_RECORD,
        arguments={
            "meal_id": 10,
            "description": "점심 기록 정정",
            "patient_id": "forged-patient",
            "expected_version": 999,
        },
        source_event_type="nutrition_management_agent",
        tool_call_id="tool-call-update-technical-001",
    )

    assert result.status == "error"
    assert result.error == "tool_permission_denied"
    assert client.record_requests == []


@pytest.mark.asyncio
async def test_policy_query_returns_public_id_from_trusted_patient_context() -> None:
    queries = FakeBackendQueryTools()
    server = AgentMcpToolServer(
        backend_client=FakeBackendV12Client(),
        backend_queries=queries,
    )

    result = await execute(
        server,
        name=GET_NOTIFICATION_POLICIES,
        arguments={"slot_label": "아침"},
        source_event_type="multiturn_chat",
        tool_call_id="tool-call-policy-query-001",
    )

    assert result.status == "success"
    assert result.response["policies"][0]["policy_id"] == "npol_0123456789abcdef"


@pytest.mark.asyncio
async def test_v12_does_not_fall_back_to_legacy_endpoint_for_unsupported_write_tool() -> None:
    client = FakeBackendV12Client()
    server = AgentMcpToolServer(
        backend_client=client,
        backend_queries=FakeBackendQueryTools(),
    )

    result = await execute(
        server,
        name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
        arguments={
            "symptom_text": "메스꺼움",
            "suspected": True,
            "severity": "low",
        },
        source_event_type="medication_agent",
        tool_call_id="tool-call-side-effect-001",
    )

    assert result.status == "error"
    assert result.error == "backend_v12_write_tool_not_supported"
    assert client.record_requests == []
