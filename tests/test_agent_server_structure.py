from __future__ import annotations

import asyncio
import json

from fastapi import Request

from agent_app.errors import AgentExecutionError
from agent_app.llm.messages import langchain_tools_from_catalog
from agent_app.main import (
    agent_execution_error_handler,
    app as agent_app,
)
from agent_app.providers.base import BaseLLMProvider
from shared.tool_catalog import ToolCatalog
from shared.tool_permissions import requires_human_handoff
from agent_app.tools.policy import DEFERRED_POLICY_TOOL_NAMES
from agent_app.tools.protocol import ALLOWED_TOOL_NAMES
from agent_app.tools.runtime import ToolRuntime
from shared.retention_policy import (
    AGENT_OBSERVABILITY_RETENTION_DAYS,
    AGENT_OBSERVABILITY_RETENTION_SECONDS,
)
from shared.schemas import ToolCallResult
from shared.tool_names import (
    GET_SIDE_EFFECT_HISTORY,
    SOURCE_MEDICATION_AGENT,
)


def _routes() -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    for path, operations in agent_app.openapi()["paths"].items():
        for method in operations:
            upper_method = method.upper()
            if upper_method in {"GET", "POST"}:
                rows.add((upper_method, path))
    return rows


def test_agent_server_openapi_exposes_only_active_public_routes():
    routes = _routes()

    expected = {
        ("POST", "/agent/sync/chat"),
        ("POST", "/agent/async/chat_feedback"),
        ("POST", "/agent/async/missed-dose-events"),
        (
            "POST",
            "/agent/async/daily-medication-pattern-analysis",
        ),
    }

    assert routes == expected


def test_global_agent_error_response_hides_internal_trace_context():
    request_body = json.dumps(
        {"request_id": "req_0000000012345678"},
    ).encode("utf-8")
    received = False

    async def receive():
        nonlocal received
        if received:
            return {
                "type": "http.request",
                "body": b"",
                "more_body": False,
            }
        received = True
        return {
            "type": "http.request",
            "body": request_body,
            "more_body": False,
        }

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/agent/internal-test",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        },
        receive,
    )
    response = asyncio.run(
        agent_execution_error_handler(
            request,
            AgentExecutionError(
                "provider included a sensitive diagnostic",
                error_type="provider_secret_error",
                trace_id="trace_internal",
                agent_name="internal_agent",
                decision_type="internal_decision",
            ),
        )
    )
    body = json.loads(response.body)

    assert response.status_code == 500
    assert body == {
        "request_id": "req_0000000012345678",
        "error": {
            "code": "AI_PROCESSING_ERROR",
            "message": (
                "An internal AI Server processing error occurred."
            ),
            "retryable": True,
            "details": None,
        },
    }
    serialized = json.dumps(body)
    assert "trace_internal" not in serialized
    assert "internal_agent" not in serialized
    assert "internal_decision" not in serialized
    assert "provider included" not in serialized


def test_tool_catalog_protocol_allowlist_and_handoff_flags_are_aligned():
    catalog = ToolCatalog.available_tools_payload()
    names = {str(tool["name"]) for tool in catalog}

    assert names == ALLOWED_TOOL_NAMES
    assert DEFERRED_POLICY_TOOL_NAMES <= names
    assert {name for name in names if requires_human_handoff(name)} == DEFERRED_POLICY_TOOL_NAMES
    deferred_tools = {str(tool["name"]): tool for tool in catalog if str(tool["name"]) in DEFERRED_POLICY_TOOL_NAMES}
    for tool in deferred_tools.values():
        meta = tool.get("_meta")
        assert isinstance(meta, dict)
        assert meta["execution_mode"] == "deferred_confirmation"
        assert meta["requires_human_handoff"] is True
        assert meta["handoff_gate"] == "high_risk_policy_change"
    for tool in catalog:
        assert isinstance(tool.get("inputSchema"), dict)
        assert isinstance(tool.get("outputSchema"), dict)
        assert tool.get("description")


def test_multiturn_tools_bind_through_chat_model_tool_specs():
    tools = langchain_tools_from_catalog(ToolCatalog.available_tools_payload())
    by_name = {tool["function"]["name"]: tool for tool in tools}

    assert not hasattr(BaseLLMProvider, "bind_tools")
    assert not hasattr(BaseLLMProvider, "generate_json")
    assert {"update_medication_dose_event_status", "create_nutrition_meal_record", "get_nutrition_recommendation_candidates"} <= set(by_name)
    assert by_name["update_medication_dose_event_status"]["type"] == "function"
    assert by_name["update_medication_dose_event_status"]["function"]["parameters"]["type"] == "object"
    assert "dose_event_id" in by_name["update_medication_dose_event_status"]["function"]["parameters"]["required"]


def test_tool_runtime_delegates_authorized_calls_to_executor_boundary():
    class CapturingExecutor:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            self.calls.append(
                {
                    "tool_call": tool_call,
                    "trace_id": trace_id,
                    "source_event_type": source_event_type,
                    "payload": payload,
                }
            )
            return ToolCallResult(
                tool_name=str(tool_call.get("name") or "unknown"),
                status="success",
                response={"delegated": True},
                idempotency_key=f"{trace_id}:delegated",
            )

    executor = CapturingExecutor()
    runtime = ToolRuntime(executor)

    executed_calls, results = asyncio.run(
        runtime.execute(
            [
                {
                    "name": GET_SIDE_EFFECT_HISTORY,
                    "arguments": {"limit": 5},
                }
            ],
            trace_id="structure-test-trace",
            source_event_type=SOURCE_MEDICATION_AGENT,
            payload={},
        )
    )

    assert executed_calls[0]["name"] == GET_SIDE_EFFECT_HISTORY
    assert len(executor.calls) == 1
    assert (
        executor.calls[0]["source_event_type"]
        == SOURCE_MEDICATION_AGENT
    )
    assert results[0].status == "success"


def test_tool_runtime_logs_routing_context(monkeypatch):
    events: list[dict] = []

    class CapturingExecutor:
        async def execute_tool_call(self, tool_call, *, trace_id, source_event_type, payload):
            return ToolCallResult(
                tool_name=str(tool_call.get("name") or "unknown"),
                status="success",
                response={"ok": True},
                idempotency_key=f"{trace_id}:delegated",
            )

    def capture_log(event: str, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr("agent_app.tools.runtime.trace_logging.log_info", capture_log)

    runtime = ToolRuntime(CapturingExecutor())
    asyncio.run(
        runtime.execute(
            [{"name": "update_medication_dose_event_status", "arguments": {"dose_event_id": 12}}],
            trace_id="routing-log-trace",
            source_event_type="multiturn_chat",
            payload={},
            routing_context={
                "routing_mode": "delegated_agent",
                "executed_by": "medication_agent",
                "supervisor_agent": "multiturn_chat_agent",
                "specialist_agent": "medication_agent",
                "tool_loop_mode": "langgraph_state_graph",
                "supervisor_tool_names": ["delegate_to_medication_agent"],
                "specialist_tool_names": ["update_medication_dose_event_status"],
            },
        )
    )

    started = next(event for event in events if event["event"] == "agent_tool_call_started")

    assert started["routing"]["routing_mode"] == "delegated_agent"
    assert started["routing"]["executed_by"] == "medication_agent"
    assert started["routing"]["tool_loop_mode"] == "langgraph_state_graph"
    assert started["routing"]["supervisor_tool_names"] == ["delegate_to_medication_agent"]
    assert started["routing"]["specialist_tool_names"] == ["update_medication_dose_event_status"]


def test_agent_observability_retention_is_fixed_to_three_years():
    assert AGENT_OBSERVABILITY_RETENTION_DAYS == 1095
    assert AGENT_OBSERVABILITY_RETENTION_SECONDS == 94_608_000
