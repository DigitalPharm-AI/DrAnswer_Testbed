from __future__ import annotations

import json
from pathlib import Path

from system_app.openapi_v13 import (
    build_backend_v13_async_callback_openapi,
    build_backend_v13_write_openapi,
    install_system_v13_openapi,
)
from tools.export_backend_v13_openapi import contract_app

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_backend_v13_write_openapi_contains_sync_write_paths() -> None:
    specification = build_backend_v13_write_openapi(contract_app())

    assert specification["info"]["version"] == "1.3"
    assert set(specification["paths"]) == {
        "/agent/sync/record-change",
        "/agent/sync/notification-policy-change",
    }
    for path in specification["paths"]:
        operation = specification["paths"][path]["post"]
        assert operation["security"] == [{"BackendApiBearer": []}]
        assert set(operation["responses"]) == {
            "200",
            "400",
            "401",
            "404",
            "409",
            "422",
            "500",
        }

    components = specification["components"]["schemas"]
    for schema_name in (
        "RecordChangeRequest",
        "NotificationPolicyChangeRequest",
    ):
        schema = components[schema_name]
        assert schema["additionalProperties"] is False
        assert "conversation_id" not in schema["properties"]


def test_backend_v13_callback_openapi_contains_required_callbacks() -> None:
    specification = build_backend_v13_async_callback_openapi(
        contract_app()
    )

    assert specification["info"]["version"] == "1.3"
    assert set(specification["paths"]) == {
        "/api/agent/async/missed-dose-results",
        "/api/agent/async/notification-policy-change-proposals",
    }
    for path in specification["paths"]:
        operation = specification["paths"][path]["post"]
        assert operation["security"] == [{"BackendApiBearer": []}]

    missed = specification["paths"][
        "/api/agent/async/missed-dose-results"
    ]["post"]
    proposal = specification["paths"][
        "/api/agent/async/notification-policy-change-proposals"
    ]["post"]
    assert set(missed["responses"]) == {
        "200",
        "400",
        "401",
        "404",
        "409",
        "500",
    }
    assert set(proposal["responses"]) == {
        "200",
        "202",
        "400",
        "401",
        "409",
        "422",
        "500",
    }

    schemas = specification["components"]["schemas"]
    assert schemas["MissedDoseResultCallback"][
        "additionalProperties"
    ] is False
    assert schemas["NotificationPolicyChangeProposalRequest"][
        "additionalProperties"
    ] is False


def test_backend_v13_openapi_uses_only_16_hex_public_ids() -> None:
    specifications = (
        build_backend_v13_write_openapi(contract_app()),
        build_backend_v13_async_callback_openapi(contract_app()),
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
        "^dose_[0-9a-f]{16}$",
        "^npol_[0-9a-f]{16}$",
    ):
        assert pattern in payload


def test_backend_v13_install_overlays_public_info_without_main_switch() -> None:
    app = contract_app()
    install_system_v13_openapi(app)

    specification = app.openapi()
    assert specification["info"] == {
        "title": "닥터앤서 Backend v1.3 연동 API",
        "version": "1.3",
    }


def test_backend_v13_openapi_artifacts_match_router_contracts() -> None:
    app = contract_app()
    expected = {
        "BACKEND_V13_WRITE_OPENAPI.json": (
            build_backend_v13_write_openapi(app)
        ),
        "BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json": (
            build_backend_v13_async_callback_openapi(app)
        ),
    }

    for filename, specification in expected.items():
        exported = json.loads(
            (PROJECT_ROOT / "docs" / filename).read_text(
                encoding="utf-8"
            )
        )
        assert exported == specification


def test_backend_v13_exporter_does_not_import_runtime_app() -> None:
    source = (
        PROJECT_ROOT / "tools" / "export_backend_v13_openapi.py"
    ).read_text(encoding="utf-8")
    assert "from system_app.main import" not in source
