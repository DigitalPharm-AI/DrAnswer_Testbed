from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from fastapi import FastAPI

from agent_app.routes.chat import SYNC_CHAT_PATH
from agent_app.routes.feedback import FEEDBACK_API_PATH

SCHEMA_REF_PREFIX = "#/components/schemas/"


def build_agent_v12_chat_openapi(app: FastAPI) -> dict[str, Any]:
    """Export the Backend-to-AI chat and feedback boundary as a standalone contract."""

    source = app.openapi()
    paths = {
        path: deepcopy(source["paths"][path])
        for path in (SYNC_CHAT_PATH, FEEDBACK_API_PATH)
    }
    _remove_fastapi_validation_response(paths)
    schemas = _referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 AI Server v1.2 채팅 및 피드백 연동 API",
            "version": "1.2",
            "description": (
                "Backend Server가 저장된 사용자 message_id와 채팅 컨텍스트를 전달하고 "
                "AI Server가 검증된 응답을 동기로 반환하며, 검증된 AI 답변에 대한 "
                "암호화 피드백을 비동기로 접수하는 API 계약이다."
            ),
        },
        "paths": paths,
        "components": {
            "schemas": schemas,
            "securitySchemes": source.get("components", {}).get("securitySchemes", {}),
        },
        "x-data-boundary": {
            "message_id": "Backend DB에 먼저 저장된 user chat_messages.public_id (opaque string)",
            "backend_read": "AI Server의 고정 파라미터 쿼리 Tool만 read-only DB 계정으로 수행",
            "backend_write": "AI Server Tool이 Backend v1.2 write API를 동기로 호출",
            "agent_state": "trace, tool execution, idempotency state는 AI 내부 DB에만 저장",
            "feedback": (
                "AI 답변 message_id와 환자·대화를 Backend read view로 검증하고 "
                "자유문은 AI DB에 AES-256-GCM ciphertext로만 저장"
            ),
            "llm_direct_access": False,
        },
    }


def install_agent_v12_openapi(app: FastAPI) -> None:
    """Keep the live app schema aligned with its 400 validation handler."""

    source_openapi = app.openapi

    def contract_aware_openapi() -> dict[str, Any]:
        schema = source_openapi()
        _remove_fastapi_validation_response(schema.get("paths", {}))
        return schema

    app.openapi = contract_aware_openapi


def _remove_fastapi_validation_response(paths: dict[str, Any]) -> None:
    for path in (SYNC_CHAT_PATH, FEEDBACK_API_PATH):
        operation = paths.get(path, {}).get("post", {})
        responses = operation.get("responses")
        if isinstance(responses, dict):
            # FastAPI automatically publishes 422 for request models, while
            # the v1.2 exception handler converts those branches to 400.
            responses.pop("422", None)


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
