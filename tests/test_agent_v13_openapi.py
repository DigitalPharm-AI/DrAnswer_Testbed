from __future__ import annotations

import json
from pathlib import Path

from agent_app.openapi_v13 import (
    DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    build_agent_v13_async_medication_openapi,
    build_agent_v13_chat_openapi,
    install_agent_v13_openapi,
)
from tools.export_agent_v13_openapi import contract_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_agent_v13_chat_openapi_is_versioned_ndjson_contract() -> None:
    specification = build_agent_v13_chat_openapi(contract_app())

    assert specification["info"]["version"] == "1.3"
    assert set(specification["paths"]) == {
        "/agent/sync/chat",
        "/agent/async/chat_feedback",
    }
    operation = specification["paths"]["/agent/sync/chat"]["post"]
    assert operation["security"] == [{"AgentSyncBearer": []}]
    assert operation["parameters"] == [
        {
            "name": "Accept",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "title": "Accept"},
        }
    ]
    assert set(operation["responses"]["200"]["content"]) == {
        "application/x-ndjson"
    }
    assert operation["responses"]["200"]["content"][
        "application/x-ndjson"
    ]["schema"]["x-ndjson-item-schema"] == {
        "$ref": "#/components/schemas/ChatStreamEvent"
    }
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
    request_schema = specification["components"]["schemas"][
        "ChatSyncRequest"
    ]
    assert request_schema["additionalProperties"] is False
    assert "conversation_id" not in request_schema["properties"]
    assert "requested_return_type" in request_schema["required"]


def test_agent_v13_feedback_and_structured_output_schemas_are_strict() -> None:
    schemas = build_agent_v13_chat_openapi(
        contract_app()
    )["components"]["schemas"]

    feedback = schemas["ChatFeedbackRequest"]
    assert feedback["additionalProperties"] is False
    assert "conversation_id" not in feedback["properties"]
    assert "feedback" not in feedback["properties"]
    assert {"request_id", "patient_id", "message_id"}.issubset(
        feedback["required"]
    )
    assert feedback["anyOf"] == [
        {
            "required": ["reaction"],
            "properties": {
                "reaction": {
                    "type": "string",
                    "enum": ["like", "dislike"],
                }
            },
        },
        {
            "required": ["feedback_text"],
            "properties": {
                "feedback_text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                }
            },
        },
    ]

    event_branches = schemas["ChatStreamEvent"]["oneOf"]
    assert [
        branch["properties"]["status"]["const"]
        for branch in event_branches
    ] == [
        "streaming",
        "completed",
        "completed",
        "completed",
        "error",
    ]
    assert [
        branch["properties"]["message_type"]["const"]
        for branch in event_branches
        if branch["properties"]["status"]["const"] == "completed"
    ] == ["text", "selection_box", "input_box"]

    input_branches = schemas["ChatInput"]["oneOf"]
    assert [
        branch["properties"]["type"]["const"]
        for branch in input_branches
    ] == ["number", "dropdown"]
    number_overlay = input_branches[0]["properties"]["options"][
        "allOf"
    ][1]
    assert set(number_overlay["required"]) == {
        "unit",
        "lower",
        "upper",
    }
    dropdown_overlay = input_branches[1]["properties"]["options"][
        "allOf"
    ][1]
    assert dropdown_overlay["properties"]["selections"][
        "minItems"
    ] == 1


def test_agent_v13_async_openapi_includes_both_acceptance_paths() -> None:
    specification = build_agent_v13_async_medication_openapi(
        contract_app()
    )

    assert specification["info"]["version"] == "1.3"
    assert set(specification["paths"]) == {
        "/agent/async/missed-dose-events",
        DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    }
    for path in specification["paths"]:
        operation = specification["paths"][path]["post"]
        assert operation["security"] == [{"AgentSyncBearer": []}]
        assert set(operation["responses"]) == {
            "200",
            "202",
            "400",
            "401",
            "409",
            "500",
            "503",
        }

    daily_schema = specification["components"]["schemas"][
        "DailyMedicationPatternAnalysisRequest"
    ]
    assert daily_schema["additionalProperties"] is False
    assert set(daily_schema["properties"]) == {
        "request_id",
        "patient_id",
        "analysis_date",
    }
    assert set(daily_schema["required"]) == {
        "request_id",
        "patient_id",
        "analysis_date",
    }
    patient_ids = daily_schema["properties"]["patient_id"]
    assert patient_ids["type"] == "array"
    assert patient_ids["items"]["pattern"] == (
        "^patient_[0-9a-f]{16}$"
    )

    missed_schema = specification["components"]["schemas"][
        "MissedDoseEventRequest"
    ]
    assert missed_schema["additionalProperties"] is False
    assert set(missed_schema["properties"]) == {
        "request_id",
        "patient_id",
        "dose_event_id",
    }
    assert set(missed_schema["required"]) == {
        "request_id",
        "patient_id",
        "dose_event_id",
    }


def test_agent_v13_openapi_uses_only_16_hex_public_ids() -> None:
    specifications = (
        build_agent_v13_chat_openapi(contract_app()),
        build_agent_v13_async_medication_openapi(contract_app()),
    )
    payload = json.dumps(
        specifications,
        ensure_ascii=False,
        sort_keys=True,
    )

    assert "[0-9a-f]{8}" not in payload
    for pattern in (
        "^req_[0-9a-f]{16}$",
        "^patient_[0-9a-f]{16}$",
        "^user_msg_[0-9a-f]{16}$",
        "^assistant_msg_[0-9a-f]{16}$",
        "^dose_[0-9a-f]{16}$",
    ):
        assert pattern in payload


def test_agent_v13_install_overlays_public_info_without_main_switch() -> None:
    app = contract_app()
    install_agent_v13_openapi(app)

    specification = app.openapi()
    assert specification["info"] == {
        "title": "닥터앤서 AI Server v1.3 연동 API",
        "version": "1.3",
    }
    assert set(
        specification["paths"]["/agent/sync/chat"]["post"][
            "responses"
        ]["200"]["content"]
    ) == {"application/x-ndjson"}


def test_agent_v13_openapi_artifacts_match_router_contracts() -> None:
    app = contract_app()
    expected = {
        "AI_V13_CHAT_OPENAPI.json": build_agent_v13_chat_openapi(app),
        "AI_V13_ASYNC_MEDICATION_OPENAPI.json": (
            build_agent_v13_async_medication_openapi(app)
        ),
    }

    for filename, specification in expected.items():
        exported = json.loads(
            (PROJECT_ROOT / "docs" / filename).read_text(
                encoding="utf-8"
            )
        )
        assert exported == specification


def test_agent_v13_exporter_does_not_import_runtime_app() -> None:
    source = (
        PROJECT_ROOT / "tools" / "export_agent_v13_openapi.py"
    ).read_text(encoding="utf-8")
    assert "from agent_app.main import" not in source
