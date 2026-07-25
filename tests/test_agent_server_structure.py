from __future__ import annotations

import asyncio

from agent_app.llm.messages import langchain_tools_from_catalog
from agent_app.main import app as agent_app
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.permissions import requires_human_handoff
from agent_app.tools.policy import DEFERRED_POLICY_TOOL_NAMES
from agent_app.tools.protocol import ALLOWED_TOOL_NAMES
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import ToolCallResult
from system_app.services import observability_view, trace_retention


def _routes() -> set[tuple[str, str]]:
    rows: set[tuple[str, str]] = set()
    for path, operations in agent_app.openapi()["paths"].items():
        for method in operations:
            upper_method = method.upper()
            if upper_method in {"GET", "POST"}:
                rows.add((upper_method, path))
    return rows


def test_agent_server_api_contract_routes_are_present():
    routes = _routes()

    expected = {
        ("GET", "/agent/model-config"),
        ("POST", "/agent/model-config"),
        ("POST", "/agent/multiturn-chat"),
        ("POST", "/agent/async/daily-patterns"),
        ("POST", "/agent/async/missed-dose-events"),
        ("POST", "/agent/async/chat-continuations"),
        ("POST", "/agent/async/push-messages"),
        ("POST", "/agent/async/clinician-alerts"),
        ("POST", "/agent/mcp"),
        ("GET", "/agent/async/tasks/status"),
        ("GET", "/agent/ops/readiness"),
        ("GET", "/agent/async/tasks"),
        ("GET", "/agent/async/tasks/dead"),
        ("POST", "/agent/async/tasks/{request_id}/actions"),
        ("GET", "/agent/async/tasks/{request_id}"),
    }

    assert expected <= routes


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


def test_tool_runtime_delegates_permission_decisions_to_executor_boundary():
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
            [{"name": "create_nutrition_meal_record", "arguments": {"meal_type": "lunch", "foods": []}}],
            trace_id="structure-test-trace",
            source_event_type="missed_dose",
            payload={},
        )
    )

    assert executed_calls[0]["name"] == "create_nutrition_meal_record"
    assert len(executor.calls) == 1
    assert executor.calls[0]["source_event_type"] == "missed_dose"
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


def test_trace_retention_policy_is_single_source_for_logs_view():
    assert observability_view.TRACE_RETENTION_POLICY is trace_retention.TRACE_RETENTION_POLICY
    assert observability_view._trace_retention_dry_run_count.__module__ == "system_app.services.observability_view"
