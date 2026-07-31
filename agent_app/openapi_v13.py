from __future__ import annotations

from copy import deepcopy
from typing import Any

from fastapi import FastAPI

from agent_app.openapi_schema import (
    apply_conditional_contract_schemas,
    keep_only_ndjson_chat_success,
)
from agent_app.routes.chat import SYNC_CHAT_PATH
from agent_app.routes.feedback import FEEDBACK_API_PATH
from agent_app.routes.tasks import (
    DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    MISSED_DOSE_EVENT_PATH,
)
from shared.openapi_schema import referenced_schemas
AGENT_V13_CHAT_PATHS = (
    SYNC_CHAT_PATH,
    FEEDBACK_API_PATH,
)
AGENT_V13_ASYNC_MEDICATION_PATHS = (
    MISSED_DOSE_EVENT_PATH,
    DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
)
AGENT_V13_ACTIVE_PATHS = (
    *AGENT_V13_CHAT_PATHS,
    *AGENT_V13_ASYNC_MEDICATION_PATHS,
)


def build_agent_v13_chat_openapi(app: FastAPI) -> dict[str, Any]:
    """Export the v1.3 Backend-to-AI chat and feedback boundary."""

    source = app.openapi()
    paths = _selected_paths(source, AGENT_V13_CHAT_PATHS)
    _remove_v13_validation_responses(paths)
    keep_only_ndjson_chat_success(
        paths,
        sync_chat_path=SYNC_CHAT_PATH,
    )
    schemas = referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    apply_conditional_contract_schemas(schemas)
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 AI Server v1.3 채팅 및 피드백 연동 API",
            "version": "1.3",
            "description": (
                "Backend Server가 먼저 저장한 사용자 message_id와 환자 단일 "
                "스레드 컨텍스트를 전달하고, AI Server가 검증된 답변을 "
                "application/x-ndjson으로 반환하는 동기 채팅 계약이다. "
                "모든 공개 식별자는 종류별 prefix와 16자리 lowercase hex "
                "접미사를 사용한다."
            ),
        },
        "paths": paths,
        "components": _components(source, schemas),
        "x-data-boundary": {
            "contract_version": "1.3",
            "public_id_suffix": "16 lowercase hexadecimal characters",
            "thread_scope": "환자당 하나의 채팅 스레드",
            "message_id": (
                "Backend DB에 먼저 저장된 chat_messages.public_id "
                "(user_msg_* 또는 assistant_msg_*)"
            ),
            "backend_read": (
                "AI Server의 고정 파라미터 Query Tool만 read-only "
                "Backend DB 계정으로 수행"
            ),
            "backend_write": (
                "AI Server의 쓰기 Tool이 Backend v1.3 동기 write API를 호출"
            ),
            "agent_state": (
                "trace, tool 실행 상태, 멱등성 상태는 AI 내부 DB에만 저장"
            ),
            "feedback": (
                "AI 답변 message_id와 patient_id를 Backend read view로 "
                "검증하고 반응과 선택적 자유 의견을 접수"
            ),
            "llm_direct_database_access": False,
        },
    }


def build_agent_v13_async_medication_openapi(
    app: FastAPI,
) -> dict[str, Any]:
    """Export v1.3 missed-dose and daily-pattern acceptance endpoints."""

    source = app.openapi()
    paths = _selected_paths(
        source,
        AGENT_V13_ASYNC_MEDICATION_PATHS,
    )
    _remove_v13_validation_responses(paths)
    schemas = referenced_schemas(
        paths,
        source.get("components", {}).get("schemas", {}),
    )
    return {
        "openapi": source.get("openapi", "3.1.0"),
        "info": {
            "title": "닥터앤서 AI Server v1.3 비동기 복약 이벤트 API",
            "version": "1.3",
            "description": (
                "Backend Server가 미복용 이벤트 또는 분석 대상 환자 목록을 "
                "AI Server에 멱등하게 접수하는 v1.3 비동기 계약이다. 상세 "
                "환자 컨텍스트는 AI Server가 읽기 전용 Backend DB 조회로 "
                "보강한다."
            ),
        },
        "paths": paths,
        "components": _components(source, schemas),
        "x-contract-scope": {
            "contract_version": "1.3",
            "public_id_suffix": "16 lowercase hexadecimal characters",
            "included": list(AGENT_V13_ASYNC_MEDICATION_PATHS),
            "daily_pattern_trigger": (
                "Backend가 analysis_date와 분석 대상 patient_id 목록을 전달"
            ),
        },
    }


def install_agent_v13_openapi(app: FastAPI) -> None:
    """Apply v1.3 public metadata and conditional schema overlays."""

    source_openapi = app.openapi

    def contract_aware_openapi() -> dict[str, Any]:
        schema = source_openapi()
        info = schema.setdefault("info", {})
        info["version"] = "1.3"
        info["title"] = "닥터앤서 AI Server v1.3 연동 API"
        paths = schema.get("paths", {})
        _remove_v13_validation_responses(paths)
        keep_only_ndjson_chat_success(
            paths,
            sync_chat_path=SYNC_CHAT_PATH,
        )
        apply_conditional_contract_schemas(
            schema.get("components", {}).get("schemas", {})
        )
        return schema

    app.openapi = contract_aware_openapi


def _selected_paths(
    source: dict[str, Any],
    required_paths: tuple[str, ...],
) -> dict[str, Any]:
    available = source.get("paths", {})
    missing = [path for path in required_paths if path not in available]
    if missing:
        raise KeyError(
            "missing_v13_openapi_paths:" + ",".join(missing)
        )
    return {
        path: deepcopy(available[path])
        for path in required_paths
    }


def _remove_v13_validation_responses(
    paths: dict[str, Any],
) -> None:
    for path in AGENT_V13_ACTIVE_PATHS:
        responses = (
            paths.get(path, {})
            .get("post", {})
            .get("responses")
        )
        if isinstance(responses, dict):
            responses.pop("422", None)


def _components(
    source: dict[str, Any],
    schemas: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schemas": schemas,
        "securitySchemes": deepcopy(
            source.get("components", {}).get("securitySchemes", {})
        ),
    }
