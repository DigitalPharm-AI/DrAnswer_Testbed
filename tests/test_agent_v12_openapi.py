from __future__ import annotations

import json
from pathlib import Path

from agent_app import main as agent_main
from agent_app.openapi_v12 import build_agent_v12_chat_openapi


def test_agent_v12_openapi_declares_security_and_contract_errors() -> None:
    specification = agent_main.app.openapi()
    operation = specification["paths"]["/agent/sync/chat"]["post"]

    assert specification["components"]["securitySchemes"]["AgentSyncBearer"]["scheme"] == "bearer"
    assert operation["security"] == [{"AgentSyncBearer": []}]
    assert "parameters" not in operation
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "404",
        "409",
        "500",
        "503",
        "504",
    }
    for status in ("400", "401", "404", "409", "500", "503", "504"):
        schema = operation["responses"][status]["content"]["application/json"]["schema"]
        assert schema["$ref"] == "#/components/schemas/ChatErrorResponse"


def test_agent_feedback_openapi_declares_202_security_and_errors() -> None:
    specification = agent_main.app.openapi()
    operation = specification["paths"]["/agent/async/chat_feedback"]["post"]

    assert operation["security"] == [{"AgentSyncBearer": []}]
    assert "parameters" not in operation
    assert set(operation["responses"]) == {
        "202",
        "400",
        "401",
        "404",
        "409",
        "500",
        "503",
    }
    accepted = operation["responses"]["202"]["content"][
        "application/json"
    ]["schema"]
    assert accepted["$ref"] == "#/components/schemas/ChatFeedbackAccepted"
    request = operation["requestBody"]["content"]["application/json"][
        "schema"
    ]
    assert request["$ref"] == "#/components/schemas/ChatFeedbackRequest"
    for status in ("400", "401", "404", "409", "500", "503"):
        schema = operation["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert schema["$ref"] == "#/components/schemas/ChatErrorResponse"


def test_agent_v12_exported_openapi_matches_runtime_contract() -> None:
    exported = json.loads(Path("docs/AI_V12_CHAT_OPENAPI.json").read_text(encoding="utf-8"))
    assert exported == build_agent_v12_chat_openapi(agent_main.app)
