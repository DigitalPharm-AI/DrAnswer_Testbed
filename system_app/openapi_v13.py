from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from shared.openapi_schema import (
    openapi_components,
    referenced_schemas,
    remove_openapi_response,
    selected_openapi_paths,
)
from system_app.services.backend_v13_service import (
    POLICY_CHANGE_PATH,
    RECORD_CHANGE_PATH,
)

BACKEND_V13_WRITE_PATHS = (
    RECORD_CHANGE_PATH,
    POLICY_CHANGE_PATH,
)
BACKEND_V13_ASYNC_CALLBACK_PATHS = (
    "/api/agent/async/missed-dose-results",
    "/api/agent/async/notification-policy-change-proposals",
)
BACKEND_V13_NO_422_PATHS = BACKEND_V13_ASYNC_CALLBACK_PATHS[:1]


def build_backend_v13_write_openapi(
    app: FastAPI,
) -> dict[str, Any]:
    """Export the v1.3 AI-to-Backend synchronous write boundary."""

    source = app.openapi()
    paths = selected_openapi_paths(source, BACKEND_V13_WRITE_PATHS)
    schemas = referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 Backend v1.3 AI 쓰기 연동 API",
            "version": "1.3",
            "description": (
                "AI Server가 사용자 확인을 마친 업무 데이터 변경을 "
                "Backend Server에 동기로 요청하는 v1.3 계약이다. 공개 "
                "식별자는 종류별 prefix와 16자리 lowercase hex 접미사를 "
                "사용한다."
            ),
        },
        "paths": paths,
        "components": openapi_components(source, schemas),
        "x-ai-server-tool-boundary": {
            "contract_version": "1.3",
            "public_id_suffix": "16 lowercase hexadecimal characters",
            "model_arguments": [
                "target business record identifiers",
                "requested business changes",
                "policy decision",
            ],
            "tool_managed_arguments": [
                "patient_id",
                "expected_version",
                "request_id",
                "source_chat_request_id",
                "confirmation_message_id",
                "requested_at",
                "reason",
            ],
            "notification_policy_id": (
                "Backend가 발급한 reminder_policies.public_id"
            ),
            "additional_properties": False,
        },
    }


def build_backend_v13_async_callback_openapi(
    app: FastAPI,
) -> dict[str, Any]:
    """Export the v1.3 AI-to-Backend asynchronous result callbacks."""

    source = app.openapi()
    paths = selected_openapi_paths(
        source,
        BACKEND_V13_ASYNC_CALLBACK_PATHS,
    )
    remove_openapi_response(paths, BACKEND_V13_NO_422_PATHS, "422")
    schemas = referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 Backend v1.3 비동기 결과 Callback API",
            "version": "1.3",
            "description": (
                "AI Server가 미복용 이벤트 처리 결과와 알림 정책 변경 "
                "제안을 전달하는 v1.3 Callback 계약이다. 정책 제안은 "
                "사용자 확인 전 현재 정책을 변경하지 않는다."
            ),
        },
        "paths": paths,
        "components": openapi_components(source, schemas),
        "x-contract-scope": {
            "contract_version": "1.3",
            "public_id_suffix": "16 lowercase hexadecimal characters",
            "included": list(BACKEND_V13_ASYNC_CALLBACK_PATHS),
        },
    }


def install_system_v13_openapi(app: FastAPI) -> None:
    """Apply v1.3 public metadata and callback error envelopes."""

    source_openapi = app.openapi

    def contract_aware_openapi() -> dict[str, Any]:
        schema = source_openapi()
        info = schema.setdefault("info", {})
        info["version"] = "1.3"
        info["title"] = "닥터앤서 Backend v1.3 연동 API"
        remove_openapi_response(
            schema.get("paths", {}),
            BACKEND_V13_NO_422_PATHS,
            "422",
        )
        return schema

    app.openapi = contract_aware_openapi
