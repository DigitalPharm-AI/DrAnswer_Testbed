from __future__ import annotations

from fastapi.testclient import TestClient

from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_SIDE_EFFECT_HISTORY,
    MEDICATION_CHAT_TOOLS,
    SIDE_EFFECT_TOOLS,
)
from system_app.main import app


def test_medication_read_tools_are_cataloged_for_medication_chat():
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    assert GET_MEDICATION_DOSE_STATUS in MEDICATION_CHAT_TOOLS
    assert CREATE_MEDICATION_SIDE_EFFECT_RECORD in MEDICATION_CHAT_TOOLS
    assert CREATE_MEDICATION_SIDE_EFFECT_RECORD not in SIDE_EFFECT_TOOLS
    assert tools[CREATE_MEDICATION_SIDE_EFFECT_RECORD]["_meta"]["mutability"] == "write"
    assert (
        tools[CREATE_MEDICATION_SIDE_EFFECT_RECORD]["_meta"][
            "confirmation_policy"
        ]
        == "explicit_user_message"
    )
    assert GET_SIDE_EFFECT_HISTORY in MEDICATION_CHAT_TOOLS
    assert tools[GET_MEDICATION_DOSE_STATUS]["_meta"]["mutability"] == "read"
    assert tools[GET_SIDE_EFFECT_HISTORY]["_meta"]["mutability"] == "read"


def test_direct_agent_medication_read_and_write_routes_are_removed():
    client = TestClient(app)
    responses = [
        client.get("/api/agent/dose-events"),
        client.post("/api/agent/dose-events/mark-taken", json={}),
        client.post("/api/agent/side-effects/records", json={}),
        client.get("/api/agent/side-effects/history"),
    ]

    assert [response.status_code for response in responses] == [
        404,
        404,
        404,
        404,
    ]
