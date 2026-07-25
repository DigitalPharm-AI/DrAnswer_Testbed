from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI

from system_app.services.backend_v12_service import POLICY_CHANGE_PATH, RECORD_CHANGE_PATH

BACKEND_V12_WRITE_PATHS = (RECORD_CHANGE_PATH, POLICY_CHANGE_PATH)
SCHEMA_REF_PREFIX = "#/components/schemas/"


def build_backend_v12_write_openapi(app: FastAPI) -> dict[str, Any]:
    source = app.openapi()
    paths = {path: source["paths"][path] for path in BACKEND_V12_WRITE_PATHS}
    schemas = _referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    security_schemes = source.get("components", {}).get("securitySchemes", {})
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 Backend v1.2 AI 쓰기 연동 API",
            "version": "1.2",
            "description": (
                "AI Server가 Backend 업무 데이터를 변경할 때 동기로 호출하는 API 계약이다. "
                "LLM은 업무 인자만 생성하고 식별·멱등·버전 필드는 AI Server Tool이 주입한다."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": security_schemes,
        },
        "x-ai-server-tool-boundary": {
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
                "conversation_id",
                "confirmation_message_id",
                "requested_at",
                "reason",
            ],
            "notification_policy_id": "Backend-issued opaque reminder_policies.public_id",
            "additional_properties": False,
        },
    }


def _referenced_schemas(
    root: Any,
    available_schemas: dict[str, Any],
) -> dict[str, Any]:
    pending = list(_schema_references(root))
    selected: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        schema = available_schemas.get(name)
        if schema is None:
            raise KeyError(f"missing_openapi_schema:{name}")
        selected[name] = schema
        pending.extend(_schema_references(schema))
    return {name: selected[name] for name in sorted(selected)}


def _schema_references(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith(SCHEMA_REF_PREFIX):
            yield reference.removeprefix(SCHEMA_REF_PREFIX)
        for item in value.values():
            yield from _schema_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_references(item)
