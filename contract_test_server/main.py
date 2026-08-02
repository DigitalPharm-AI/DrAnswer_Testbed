from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from contract_test_server.config import (
    ContractServerSettings,
    get_contract_server_settings,
)
from contract_test_server.models import (
    CallbackListResponse,
    CallbackReleaseResponse,
    CallbackView,
    ChatFeedbackAccepted,
    ChatFeedbackRequest,
    HealthResponse,
    ReadinessResponse,
)
from contract_test_server.storage import (
    CallbackInsert,
    ContractStore,
    canonical_request_hash,
    keyed_request_hash,
)
from shared.async_v13_contracts import (
    AsyncEventAccepted,
    DailyMedicationPatternAnalysisRequest,
    MissedDoseEventRequest,
    MissedDoseResult,
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposal,
)
from shared.backend_v13_contracts import CommonErrorResponse, ContractError
from shared.chat_contracts import (
    ChatErrorResponse,
    ChatInput,
    ChatInputOptions,
    ChatMessageContent,
    ChatStreamEvent,
    ChatSyncRequest,
)
from shared.public_ids import (
    is_public_id,
    request_id_from_body,
    request_id_from_request,
)

logger = logging.getLogger(__name__)

SYNC_CHAT_PATH = "/agent/sync/chat"
FEEDBACK_PATH = "/agent/async/chat_feedback"
MISSED_DOSE_PATH = "/agent/async/missed-dose-events"
DAILY_ANALYSIS_PATH = "/agent/async/daily-medication-pattern-analysis"
PUBLIC_CONTRACT_PATHS = frozenset(
    {
        SYNC_CHAT_PATH,
        FEEDBACK_PATH,
        MISSED_DOSE_PATH,
        DAILY_ANALYSIS_PATH,
    }
)
MISSED_DOSE_CALLBACK_PATH = "/api/agent/async/missed-dose-results"
POLICY_PROPOSAL_CALLBACK_PATH = (
    "/api/agent/async/notification-policy-change-proposals"
)

agent_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="AgentSyncBearer",
    description=(
        "Backend Server가 AI Server v1.3 API를 호출할 때 사용하는 Bearer token"
    ),
)


def _now() -> datetime:
    return datetime.now(UTC)


def _error_response(
    *,
    request_id: str | None,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return CommonErrorResponse(
        request_id=request_id,
        error=ContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        ),
    ).model_dump(mode="json")


def _raise_contract_error(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": message,
            "retryable": retryable,
            "details": details,
        },
    )


def _accepts_ndjson(value: str | None) -> bool:
    if not value:
        return False
    return any(
        item.split(";", 1)[0].strip().lower()
        == "application/x-ndjson"
        for item in value.split(",")
    )


def _json_line(value: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


async def _stream_ndjson_lines(
    lines: Sequence[bytes],
    *,
    chunk_delay_seconds: float,
) -> AsyncIterator[bytes]:
    for index, line in enumerate(lines):
        yield line
        if index < len(lines) - 1:
            await asyncio.sleep(chunk_delay_seconds)


def _split_stream_text(
    text: str,
    *,
    chunk_count: int,
) -> tuple[str, ...]:
    if not text:
        return ()
    resolved_count = max(1, min(int(chunk_count), len(text)))
    base_width, remainder = divmod(len(text), resolved_count)
    chunks: list[str] = []
    offset = 0
    for index in range(resolved_count):
        width = base_width + (1 if index < remainder else 0)
        chunks.append(text[offset : offset + width])
        offset += width
    return tuple(chunks)


def _chat_content(
    requested_return_type: str,
) -> tuple[str, ChatMessageContent]:
    if requested_return_type == "selection_box":
        return (
            "selection_box",
            ChatMessageContent(
                message_title="v1.3 계약 테스트 선택",
                text="Backend 연동 테스트를 위해 항목 하나를 선택해 주세요.",
                tables=None,
                selections=["선택 1", "선택 2"],
                inputs=None,
            ),
        )
    if requested_return_type == "input_box":
        return (
            "input_box",
            ChatMessageContent(
                message_title="v1.3 계약 테스트 입력",
                text="Backend 연동 테스트를 위해 숫자를 입력해 주세요.",
                tables=None,
                selections=None,
                inputs=[
                    ChatInput(
                        type="number",
                        label="테스트 값",
                        value=None,
                        options=ChatInputOptions(
                            unit="점",
                            lower=0,
                            upper=10,
                            selections=None,
                        ),
                    )
                ],
            ),
        )
    return (
        "text",
        ChatMessageContent(
            message_title=None,
            text=(
                "v1.3 계약 테스트 서버가 요청을 정상적으로 수신했습니다. "
                "이 응답은 실제 의료 판단이나 LLM 결과가 아닙니다."
            ),
            tables=None,
            selections=None,
            inputs=None,
        ),
    )


def _proposal_request_id(source_request_id: str, patient_id: str) -> str:
    digest = hashlib.sha256(
        (
            "notification-policy-proposal:"
            + source_request_id
            + ":"
            + patient_id
        ).encode("utf-8")
    ).hexdigest()
    return f"req_{digest[:16]}"


def create_app(
    settings: ContractServerSettings | None = None,
) -> FastAPI:
    runtime_settings = settings or get_contract_server_settings()
    runtime_settings.require_contract_postgresql()
    store = ContractStore(
        runtime_settings.contract_database_url,
        pool_size=runtime_settings.contract_db_pool_size,
        max_overflow=runtime_settings.contract_db_max_overflow,
        pool_timeout_seconds=(
            runtime_settings.contract_db_pool_timeout_seconds
        ),
        statement_timeout_ms=(
            runtime_settings.contract_db_statement_timeout_ms
        ),
        lock_timeout_ms=runtime_settings.contract_db_lock_timeout_ms,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            store.dispose()

    app = FastAPI(
        title="DRAnswer AI v1.3 Contract Test Server",
        version="1.3.0",
        description=(
            "Backend/AI 분리 개발을 위한 결정적 v1.3 wire-contract stub. "
            "실제 LLM 추론, 환자 데이터 조회, Backend 동기 쓰기는 수행하지 않습니다."
        ),
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.store = store

    def require_agent_token(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(agent_bearer),
        ] = None,
    ) -> None:
        expected = runtime_settings.agent_sync_api_token
        if not runtime_settings.agent_auth_configured:
            _raise_contract_error(
                503,
                code="SERVICE_NOT_READY",
                message="Agent API authentication is not configured.",
                retryable=True,
            )
        if (
            credentials is None
            or credentials.scheme.lower() != "bearer"
            or not hmac.compare_digest(credentials.credentials, expected)
        ):
            _raise_contract_error(
                401,
                code="UNAUTHORIZED",
                message="Authorization failed.",
            )

    def require_test_control_token(
        x_test_control_token: Annotated[
            str | None,
            Header(alias="X-Test-Control-Token"),
        ] = None,
    ) -> None:
        expected = runtime_settings.test_control_token
        if not runtime_settings.test_control_configured:
            raise HTTPException(
                status_code=503,
                detail="test_control_not_configured",
            )
        if (
            x_test_control_token is None
            or not hmac.compare_digest(x_test_control_token, expected)
        ):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request,
        exc: RequestValidationError,
    ) -> Response:
        if request.url.path not in PUBLIC_CONTRACT_PATHS:
            return await request_validation_exception_handler(request, exc)
        body = _error_response(
            request_id=request_id_from_body(exc.body),
            code="INVALID_REQUEST",
            message="Request schema or required field is invalid.",
            retryable=False,
            details={
                "violations": [
                    {
                        "location": [
                            str(value) for value in error.get("loc", ())
                        ],
                        "message": str(
                            error.get("msg") or "invalid value"
                        ),
                        "type": str(
                            error.get("type") or "validation_error"
                        ),
                    }
                    for error in exc.errors()
                ]
            },
        )
        return JSONResponse(status_code=400, content=body)

    @app.exception_handler(HTTPException)
    async def contract_http_error_handler(
        request: Request,
        exc: HTTPException,
    ) -> Response:
        if request.url.path not in PUBLIC_CONTRACT_PATHS:
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = str(
            detail.get("code")
            or ("UNAUTHORIZED" if exc.status_code == 401 else "AI_PROCESSING_ERROR")
        )
        message = str(
            detail.get("message")
            or (
                "Authorization failed."
                if exc.status_code == 401
                else "The AI contract test server could not process the request."
            )
        )
        details = detail.get("details")
        body = _error_response(
            request_id=await request_id_from_request(request),
            code=code,
            message=message,
            retryable=bool(detail.get("retryable", False)),
            details=details if isinstance(details, dict) else None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(
        request: Request,
        exc: Exception,
    ) -> Response:
        logger.exception(
            "contract_request_failed path=%s error_type=%s",
            request.url.path,
            type(exc).__name__,
        )
        if request.url.path not in PUBLIC_CONTRACT_PATHS:
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal Server Error"},
            )
        return JSONResponse(
            status_code=500,
            content=_error_response(
                request_id=await request_id_from_request(request),
                code="AI_PROCESSING_ERROR",
                message=(
                    "The AI contract test server could not process the request."
                ),
                retryable=True,
                details=None,
            ),
        )

    @app.post(
        SYNC_CHAT_PATH,
        response_class=StreamingResponse,
        dependencies=[Depends(require_agent_token)],
        responses={
            200: {
                "description": (
                    "LF-delimited ChatStreamEvent objects ending with exactly "
                    "one completed or error event."
                ),
                "content": {
                    "application/x-ndjson": {
                        "schema": {
                            "type": "string",
                            "contentMediaType": "application/x-ndjson",
                            "description": (
                                "One JSON object per LF-terminated line."
                            ),
                            "x-ndjson-item-schema": {
                                "$ref": "#/components/schemas/ChatStreamEvent"
                            },
                            "x-ndjson-framing": (
                                "one-json-object-per-lf-line"
                            ),
                        }
                    }
                },
            },
            400: {"model": ChatErrorResponse},
            401: {"model": ChatErrorResponse},
            404: {"model": ChatErrorResponse},
            409: {"model": ChatErrorResponse},
            500: {"model": ChatErrorResponse},
            503: {"model": ChatErrorResponse},
            504: {"model": ChatErrorResponse},
        },
    )
    async def sync_chat(
        payload: ChatSyncRequest,
        accept: Annotated[str | None, Header(alias="Accept")] = None,
    ) -> Response:
        if not _accepts_ndjson(accept):
            _raise_contract_error(
                400,
                code="INVALID_REQUEST",
                message="Accept must include application/x-ndjson.",
            )

        message_type, message = _chat_content(payload.requested_return_type)
        delta_text = message.text or "v1.3 계약 테스트 응답입니다."
        deltas = _split_stream_text(
            delta_text,
            chunk_count=runtime_settings.contract_stream_delta_chunks,
        )
        event_at = _now()
        terminal = ChatStreamEvent(
            request_id=payload.request_id,
            message_id=payload.message_id,
            sequence=len(deltas),
            status="completed",
            message_type=message_type,
            delta=None,
            message=message,
            error=None,
            event_at=event_at,
        )
        registration = store.register_request(
            api_path=SYNC_CHAT_PATH,
            request_id=payload.request_id,
            request_hash=canonical_request_hash(
                payload.model_dump(mode="json")
            ),
            response_status=200,
            response_json=terminal.model_dump(mode="json"),
        )
        if registration.outcome == "conflict":
            _raise_contract_error(
                409,
                code="IDEMPOTENCY_CONFLICT",
                message="The request_id was reused with a different request body.",
            )

        if registration.outcome == "replay":
            replay = ChatStreamEvent.model_validate(
                registration.response_json
            ).model_copy(update={"sequence": 0})
            lines = (
                _json_line(replay.model_dump(mode="json")),
            )
        else:
            streaming_events = tuple(
                ChatStreamEvent(
                    request_id=payload.request_id,
                    message_id=payload.message_id,
                    sequence=sequence,
                    status="streaming",
                    message_type="text",
                    delta=delta,
                    message=None,
                    error=None,
                    event_at=event_at,
                )
                for sequence, delta in enumerate(deltas)
            )
            lines = tuple(
                _json_line(event.model_dump(mode="json"))
                for event in streaming_events
            ) + (
                _json_line(terminal.model_dump(mode="json")),
            )

        return StreamingResponse(
            content=_stream_ndjson_lines(
                lines,
                chunk_delay_seconds=(
                    runtime_settings.contract_stream_chunk_delay_ms
                    / 1_000
                ),
            ),
            status_code=200,
            headers={
                "Content-Type": "application/x-ndjson; charset=utf-8",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        FEEDBACK_PATH,
        response_model=ChatFeedbackAccepted,
        status_code=202,
        dependencies=[Depends(require_agent_token)],
        responses={
            400: {"model": ChatErrorResponse},
            401: {"model": ChatErrorResponse},
            404: {"model": ChatErrorResponse},
            409: {"model": ChatErrorResponse},
            500: {"model": ChatErrorResponse},
            503: {"model": ChatErrorResponse},
        },
    )
    async def accept_feedback(
        payload: ChatFeedbackRequest,
    ) -> JSONResponse:
        accepted = ChatFeedbackAccepted(
            status="accepted",
            reaction=payload.reaction,
            accepted_at=_now(),
        )
        registration = store.register_request(
            api_path=FEEDBACK_PATH,
            request_id=payload.request_id,
            request_hash=keyed_request_hash(
                payload.model_dump(mode="json"),
                secret=runtime_settings.feedback_digest_secret,
                domain=f"v1.3-contract-request:{FEEDBACK_PATH}",
            ),
            response_status=202,
            response_json=accepted.model_dump(mode="json"),
        )
        if registration.outcome == "conflict":
            _raise_contract_error(
                409,
                code="IDEMPOTENCY_CONFLICT",
                message="The request_id was reused with a different request body.",
            )
        return JSONResponse(
            status_code=202,
            content=registration.response_json,
        )

    @app.post(
        MISSED_DOSE_PATH,
        response_model=AsyncEventAccepted,
        status_code=202,
        dependencies=[Depends(require_agent_token)],
        responses={
            200: {"model": AsyncEventAccepted},
            400: {"model": CommonErrorResponse},
            401: {"model": CommonErrorResponse},
            409: {"model": CommonErrorResponse},
            500: {"model": CommonErrorResponse},
            503: {"model": CommonErrorResponse},
        },
    )
    async def accept_missed_dose(
        payload: MissedDoseEventRequest,
    ) -> JSONResponse:
        accepted = AsyncEventAccepted(
            request_id=payload.request_id,
            status="accepted",
        )
        callback = MissedDoseResultCallback(
            request_id=payload.request_id,
            status="completed",
            result=MissedDoseResult(
                message=(
                    "v1.3 계약 테스트 미복용 결과입니다. "
                    "현재 복약 상태를 알려주세요."
                ),
                requires_reply=True,
            ),
            error=None,
        )
        registration = store.register_request(
            api_path=MISSED_DOSE_PATH,
            request_id=payload.request_id,
            request_hash=canonical_request_hash(
                payload.model_dump(mode="json")
            ),
            response_status=202,
            response_json=accepted.model_dump(mode="json"),
            callbacks=[
                CallbackInsert(
                    callback_request_id=payload.request_id,
                    source_request_id=payload.request_id,
                    callback_kind="missed_dose_result",
                    callback_path=MISSED_DOSE_CALLBACK_PATH,
                    payload=callback.model_dump(mode="json"),
                    initial_status=runtime_settings.initial_callback_status,
                )
            ],
        )
        if registration.outcome == "conflict":
            _raise_contract_error(
                409,
                code="IDEMPOTENCY_CONFLICT",
                message="The request_id was reused with a different request body.",
            )
        if registration.outcome == "replay":
            duplicate = AsyncEventAccepted(
                request_id=payload.request_id,
                status="duplicate",
            )
            return JSONResponse(
                status_code=200,
                content=duplicate.model_dump(mode="json"),
            )
        return JSONResponse(
            status_code=202,
            content=accepted.model_dump(mode="json"),
        )

    @app.post(
        DAILY_ANALYSIS_PATH,
        response_model=AsyncEventAccepted,
        status_code=202,
        dependencies=[Depends(require_agent_token)],
        responses={
            200: {"model": AsyncEventAccepted},
            400: {"model": CommonErrorResponse},
            401: {"model": CommonErrorResponse},
            409: {"model": CommonErrorResponse},
            500: {"model": CommonErrorResponse},
            503: {"model": CommonErrorResponse},
        },
    )
    async def accept_daily_analysis(
        payload: DailyMedicationPatternAnalysisRequest,
    ) -> JSONResponse:
        accepted = AsyncEventAccepted(
            request_id=payload.request_id,
            status="accepted",
        )
        callbacks: list[CallbackInsert] = []
        for patient_id in payload.patient_id:
            callback_request_id = _proposal_request_id(
                payload.request_id,
                patient_id,
            )
            proposal = NotificationPolicyChangeProposalRequest(
                request_id=callback_request_id,
                patient_id=patient_id,
                proposed_policy=NotificationPolicyProposal(
                    extra_reminders=2,
                    interval_minutes=20,
                ),
                reason=(
                    "v1.3 계약 테스트용 알림 정책 변경 제안입니다. "
                    "실제 환자 데이터 분석 결과가 아닙니다."
                ),
            )
            callbacks.append(
                CallbackInsert(
                    callback_request_id=callback_request_id,
                    source_request_id=payload.request_id,
                    callback_kind="notification_policy_proposal",
                    callback_path=POLICY_PROPOSAL_CALLBACK_PATH,
                    payload=proposal.model_dump(mode="json"),
                    initial_status=runtime_settings.initial_callback_status,
                )
            )
        registration = store.register_request(
            api_path=DAILY_ANALYSIS_PATH,
            request_id=payload.request_id,
            request_hash=canonical_request_hash(
                payload.model_dump(mode="json")
            ),
            response_status=202,
            response_json=accepted.model_dump(mode="json"),
            callbacks=callbacks,
        )
        if registration.outcome == "conflict":
            _raise_contract_error(
                409,
                code="IDEMPOTENCY_CONFLICT",
                message="The request_id was reused with a different request body.",
            )
        if registration.outcome == "replay":
            duplicate = AsyncEventAccepted(
                request_id=payload.request_id,
                status="duplicate",
            )
            return JSONResponse(
                status_code=200,
                content=duplicate.model_dump(mode="json"),
            )
        return JSONResponse(
            status_code=202,
            content=accepted.model_dump(mode="json"),
        )

    @app.get(
        "/health",
        response_model=HealthResponse,
        include_in_schema=False,
    )
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="dranswer-agent-contract-test-server",
            release=runtime_settings.app_release_version,
        )

    @app.get(
        "/health/ready",
        response_model=ReadinessResponse,
        include_in_schema=False,
    )
    async def ready() -> JSONResponse:
        database_ready = store.ready()
        errors = list(runtime_settings.readiness_errors)
        if not database_ready:
            errors.append("database_not_ready")
        ready_status = not errors
        body = ReadinessResponse(
            status="ready" if ready_status else "not_ready",
            service="dranswer-agent-contract-test-server",
            release=runtime_settings.app_release_version,
            database_ready=database_ready,
            callback_mode=runtime_settings.callback_mode,
            callback_delivery_ready=(
                runtime_settings.callback_mode == "deliver"
                and runtime_settings.callback_delivery_configured
            ),
            errors=errors,
        )
        return JSONResponse(
            status_code=200 if ready_status else 503,
            content=body.model_dump(mode="json"),
        )

    @app.get(
        "/_test/callbacks",
        response_model=CallbackListResponse,
        include_in_schema=False,
        dependencies=[Depends(require_test_control_token)],
    )
    async def list_callbacks(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ) -> CallbackListResponse:
        return CallbackListResponse(
            callbacks=[
                CallbackView.model_validate(value)
                for value in store.list_callbacks(limit=limit)
            ]
        )

    @app.post(
        "/_test/callbacks/{callback_request_id}/release",
        response_model=CallbackReleaseResponse,
        include_in_schema=False,
        dependencies=[Depends(require_test_control_token)],
    )
    async def release_callback(
        callback_request_id: str,
    ) -> CallbackReleaseResponse:
        if not is_public_id(callback_request_id, "request"):
            raise HTTPException(status_code=404, detail="callback_not_found")
        if not runtime_settings.callback_delivery_configured:
            raise HTTPException(
                status_code=409,
                detail="callback_delivery_not_configured",
            )
        outcome = store.release_callback(callback_request_id)
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="callback_not_found")
        return CallbackReleaseResponse(
            callback_request_id=callback_request_id,
            status=(
                "pending"
                if outcome == "released"
                else "already_released"
            ),
        )

    contract_files: dict[str, str] = {
        "chat.json": "AI_V13_CHAT_OPENAPI.json",
        "async-medication.json": "AI_V13_ASYNC_MEDICATION_OPENAPI.json",
        "backend-callbacks.json": "BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json",
    }

    @app.get(
        "/contracts/v1.3/{contract_name}",
        include_in_schema=False,
    )
    async def contract_artifact(contract_name: str) -> FileResponse:
        file_name = contract_files.get(contract_name)
        if file_name is None:
            raise HTTPException(status_code=404, detail="contract_not_found")
        path = Path(runtime_settings.contract_specs_dir) / file_name
        if not path.is_file():
            raise HTTPException(status_code=404, detail="contract_not_found")
        return FileResponse(
            path,
            media_type="application/json",
            filename=file_name,
        )

    original_openapi = app.openapi

    def contract_openapi() -> dict[str, Any]:
        schema = original_openapi()
        for path in PUBLIC_CONTRACT_PATHS:
            operation = schema.get("paths", {}).get(path, {}).get("post")
            if isinstance(operation, dict):
                operation.get("responses", {}).pop("422", None)
        return schema

    app.openapi = contract_openapi  # type: ignore[method-assign]
    return app


app = create_app()
