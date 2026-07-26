from __future__ import annotations

from datetime import date

import pytest

from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.mcp_server import AgentMcpToolServer
from agent_app.tools.names import (
    ALL_TOOL_NAMES,
    DELETE_NUTRITION_FOOD_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_SIDE_EFFECT_HISTORY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from agent_app.tools.permissions import TOOL_ALLOWLIST, validate_tool_permission
from agent_app.tools.protocol import tool_result_from_mcp_result

PATIENT_A = "patient-A"
PATIENT_B = "patient-B"

PATIENT_SCOPED_READ_TOOLS = {
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_SIDE_EFFECT_HISTORY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
}


class FakeBackendClient:
    pass


class CapturingBackendQueries:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _capture(self, method: str, arguments: dict) -> None:
        self.calls.append((method, arguments))

    def medication_dose_status(self, **arguments):
        self._capture("medication_dose_status", arguments)
        return {
            "success": True,
            "patient_id": arguments["patient_id"],
            "start_date": date(2026, 7, 25),
            "end_date": date(2026, 7, 25),
            "dose_events": [],
            "total": 0,
        }

    def nutrition_meals(self, **arguments):
        self._capture("nutrition_meals", arguments)
        return {"success": True, "meals": [], "total": 0}

    def daily_nutrition_summary(self, **arguments):
        self._capture("daily_nutrition_summary", arguments)
        return {"success": True, "daily_summary": {}}

    def nutrition_preferences(self, **arguments):
        self._capture("nutrition_preferences", arguments)
        return {"success": True, "preferences": {}}

    def recommendation_candidates(self, **arguments):
        self._capture("recommendation_candidates", arguments)
        return {"success": True, "recommendations": []}

    def side_effect_history(self, **arguments):
        self._capture("side_effect_history", arguments)
        return {"success": True, "records": [], "total": 0}

    def notification_policies(self, **arguments):
        self._capture("notification_policies", arguments)
        return {"success": True, "policies": [], "total": 0}

    def search_food_candidates(self, **arguments):
        self._capture("search_food_candidates", arguments)
        return {
            "success": True,
            "candidates": [],
            "query": arguments["query"],
            "limit": arguments["limit"],
        }


def _v12_payload() -> dict:
    return {
        "patient_id": PATIENT_A,
        "context": {
            "request_metadata": {
                "contract_version": "v1.2",
                "request_id": "request-001",
                "message_id": "message-001",
                "conversation_id": "conversation-001",
            }
        },
    }


async def _execute(
    server: AgentMcpToolServer,
    *,
    tool_name: str,
    arguments: dict,
    source_event_type: str,
):
    mcp_result = await server.tools_call(
        {
            "name": tool_name,
            "arguments": arguments,
            "tool_call_id": f"call-{tool_name}",
        },
        trace_id="trace-patient-scope",
        source_event_type=source_event_type,
        payload=_v12_payload(),
    )
    return tool_result_from_mcp_result(tool_name, mcp_result)


def test_patient_scoped_read_catalog_hides_patient_id_and_rejects_extra_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    for tool in tools.values():
        assert "patient_id" not in tool["required_arguments"]
        assert "patient_id" not in tool["optional_arguments"]
        assert "patient_id" not in tool["inputSchema"]["properties"]

    for tool_name in PATIENT_SCOPED_READ_TOOLS:
        tool = tools[tool_name]
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["args_schema"] == tool["inputSchema"]


def test_catalog_hides_ai_server_managed_write_arguments() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    dose_tool = tools[UPDATE_MEDICATION_DOSE_EVENT_STATUS]
    delete_food_tool = tools[DELETE_NUTRITION_FOOD_RECORD]

    assert "taken_at" not in dose_tool["optional_arguments"]
    assert "taken_at" not in dose_tool["inputSchema"]["properties"]
    assert "delete_empty_meal" not in delete_food_tool["optional_arguments"]
    assert "delete_empty_meal" not in delete_food_tool["inputSchema"]["properties"]


def test_v12_rejects_model_supplied_patient_id_for_every_executable_tool() -> None:
    tested_tools: set[str] = set()

    for source_event_type, tool_names in TOOL_ALLOWLIST.items():
        for tool_name in tool_names:
            denial = validate_tool_permission(
                {
                    "name": tool_name,
                    "arguments": {"patient_id": PATIENT_B},
                },
                source_event_type=source_event_type,
                payload=_v12_payload(),
            )
            assert denial == f"{tool_name} patient_id is managed inside the AI Server Tool"
            tested_tools.add(tool_name)

    assert tested_tools == set(ALL_TOOL_NAMES)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "source_event_type", "safe_arguments", "query_method"),
    [
        (
            GET_MEDICATION_DOSE_STATUS,
            "medication_agent",
            {"target_date": "2026-07-25"},
            "medication_dose_status",
        ),
        (
            GET_SIDE_EFFECT_HISTORY,
            "medication_agent",
            {"limit": 20},
            "side_effect_history",
        ),
        (
            GET_NUTRITION_MEAL_RECORD_LIST,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
            "nutrition_meals",
        ),
        (
            GET_NUTRITION_DAILY_SUMMARY,
            "nutrition_management_agent",
            {"meal_date": "2026-07-25"},
            "daily_nutrition_summary",
        ),
        (
            GET_NUTRITION_PREFERENCE_SUMMARY,
            "nutrition_management_agent",
            {},
            "nutrition_preferences",
        ),
        (
            GET_NUTRITION_RECOMMENDATION_CANDIDATES,
            "nutrition_recommendation_agent",
            {"constraints": {"sodium": "low"}},
            "recommendation_candidates",
        ),
        (
            GET_NOTIFICATION_POLICIES,
            "multiturn_chat",
            {"active_only": True},
            "notification_policies",
        ),
    ],
)
async def test_v12_cross_patient_argument_is_denied_and_trusted_patient_reaches_query(
    tool_name: str,
    source_event_type: str,
    safe_arguments: dict,
    query_method: str,
) -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )

    forged = await _execute(
        server,
        tool_name=tool_name,
        arguments={**safe_arguments, "patient_id": PATIENT_B},
        source_event_type=source_event_type,
    )

    assert forged.status == "error"
    assert forged.error == "tool_permission_denied"
    assert queries.calls == []

    allowed = await _execute(
        server,
        tool_name=tool_name,
        arguments=safe_arguments,
        source_event_type=source_event_type,
    )

    assert allowed.status == "success"
    assert len(queries.calls) == 1
    assert queries.calls[0][0] == query_method
    assert queries.calls[0][1]["patient_id"] == PATIENT_A
    assert PATIENT_B not in str(queries.calls[0])


@pytest.mark.asyncio
async def test_v12_food_search_rejects_patient_override_and_does_not_forward_it() -> None:
    queries = CapturingBackendQueries()
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=queries,
    )

    forged = await _execute(
        server,
        tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
        arguments={"query": "rice", "patient_id": PATIENT_B},
        source_event_type="nutrition_recommendation_agent",
    )

    assert forged.status == "error"
    assert forged.error == "tool_permission_denied"
    assert queries.calls == []

    allowed = await _execute(
        server,
        tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
        arguments={"query": "rice"},
        source_event_type="nutrition_recommendation_agent",
    )

    assert allowed.status == "success"
    assert queries.calls == [
        (
            "search_food_candidates",
            {
                "query": "rice",
                "limit": 6,
            },
        )
    ]
    assert server._patient_id_for_tool({"patient_id": PATIENT_B}, _v12_payload()) == PATIENT_A


def test_legacy_patient_argument_fallback_is_preserved_outside_v12() -> None:
    server = AgentMcpToolServer(
        backend_client=FakeBackendClient(),
        backend_queries=CapturingBackendQueries(),
    )

    assert server._patient_id_for_tool({"patient_id": PATIENT_B}, {"patient_id": PATIENT_A}) == PATIENT_B
