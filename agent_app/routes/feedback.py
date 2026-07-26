from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker

from agent_app import trace_logging
from agent_app.integration.chat_contracts import (
    ChatErrorResponse,
    chat_error,
)
from agent_app.integration.feedback_contracts import (
    ChatFeedbackAccepted,
    ChatFeedbackRequest,
)
from agent_app.integration.feedback_service import (
    FEEDBACK_API_PATH,
    ChatFeedbackService,
    FeedbackIdempotencyConflict,
    FeedbackTargetMismatch,
)
from agent_app.security import require_agent_sync_bearer_token
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
)
from shared.redaction import safe_exception_summary
from shared.settings import get_settings

router = APIRouter()
CHAT_FEEDBACK_RESPONSES = {
    400: {
        "model": ChatErrorResponse,
        "description": "Request schema or required field validation failed.",
    },
    401: {
        "model": ChatErrorResponse,
        "description": "Bearer authentication failed.",
    },
    404: {
        "model": ChatErrorResponse,
        "description": "The verified AI answer message was not found.",
    },
    409: {
        "model": ChatErrorResponse,
        "description": "The request_id was reused with another body.",
    },
    500: {
        "model": ChatErrorResponse,
        "description": "Feedback could not be accepted.",
    },
    503: {
        "model": ChatErrorResponse,
        "description": "Backend read database is unavailable or incompatible.",
    },
}

_feedback_session_factory_getter: (
    Callable[[], sessionmaker[Session]] | None
) = None
_feedback_backend_query_tools_getter: (
    Callable[[], BackendQueryTools | None] | None
) = None


def configure_feedback_session_factory(
    getter: Callable[[], sessionmaker[Session]],
) -> None:
    global _feedback_session_factory_getter
    _feedback_session_factory_getter = getter


def configure_feedback_backend_query_tools(
    getter: Callable[[], BackendQueryTools | None],
) -> None:
    global _feedback_backend_query_tools_getter
    _feedback_backend_query_tools_getter = getter


def _feedback_service() -> ChatFeedbackService:
    if _feedback_session_factory_getter is None:
        raise RuntimeError("agent_feedback_session_factory_not_configured")
    return ChatFeedbackService(
        _feedback_session_factory_getter(),
        settings=get_settings(),
    )


def _backend_query_tools() -> BackendQueryTools | None:
    if _feedback_backend_query_tools_getter is None:
        return None
    return _feedback_backend_query_tools_getter()


@router.post(
    FEEDBACK_API_PATH,
    response_model=ChatFeedbackAccepted,
    status_code=202,
    responses=CHAT_FEEDBACK_RESPONSES,
    dependencies=[Depends(require_agent_sync_bearer_token)],
)
async def chat_feedback(
    payload: ChatFeedbackRequest,
) -> JSONResponse:
    trace_logging.log_info(
        "agent_chat_feedback_received",
        path=FEEDBACK_API_PATH,
        request_id=payload.request_id,
        message_id=payload.message_id,
        conversation_id=payload.conversation_id,
        feedback_text_present=bool(payload.feedback_text),
    )
    try:
        service = _feedback_service()
    except Exception as exc:
        trace_logging.log_info(
            "agent_chat_feedback_encryption_unavailable",
            request_id=payload.request_id,
            error=safe_exception_summary(exc, limit=300),
        )
        return _feedback_error(
            status_code=503,
            code="FEEDBACK_ENCRYPTION_UNAVAILABLE",
            message="Feedback encryption is temporarily unavailable.",
            retryable=True,
        )
    try:
        replay = service.replay(payload)
    except FeedbackIdempotencyConflict:
        return _feedback_error(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The request_id was reused with a different request body.",
            retryable=False,
        )
    if replay is not None:
        return JSONResponse(
            status_code=replay.status_code,
            content=replay.body,
        )

    backend_queries = _backend_query_tools()
    if backend_queries is None:
        return _feedback_error(
            status_code=503,
            code="BACKEND_DB_UNAVAILABLE",
            message="The Backend read database is temporarily unavailable.",
            retryable=True,
        )
    try:
        verified_target = await asyncio.to_thread(
            backend_queries.validate_feedback_target,
            message_id=payload.message_id,
            conversation_id=payload.conversation_id,
            patient_id=payload.patient_id,
        )
    except BackendChatMessageNotFound:
        return _feedback_error(
            status_code=404,
            code="BACKEND_MESSAGE_NOT_FOUND",
            message="The AI answer message could not be verified.",
            retryable=False,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_chat_feedback_backend_read_failed",
            request_id=payload.request_id,
            error=safe_exception_summary(exc, limit=300),
        )
        return _feedback_error(
            status_code=503,
            code="BACKEND_DB_UNAVAILABLE",
            message="The Backend read database is temporarily unavailable.",
            retryable=True,
        )

    try:
        result = service.accept(
            payload,
            verified_target=verified_target,
        )
    except FeedbackIdempotencyConflict:
        return _feedback_error(
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            message="The request_id was reused with a different request body.",
            retryable=False,
        )
    except FeedbackTargetMismatch:
        return _feedback_error(
            status_code=404,
            code="BACKEND_MESSAGE_NOT_FOUND",
            message="The AI answer message could not be verified.",
            retryable=False,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_chat_feedback_persistence_failed",
            request_id=payload.request_id,
            error=safe_exception_summary(exc, limit=300),
        )
        return _feedback_error(
            status_code=500,
            code="FEEDBACK_ACCEPT_FAILED",
            message="The feedback request could not be accepted.",
            retryable=True,
        )

    trace_logging.log_info(
        "agent_chat_feedback_accepted",
        request_id=payload.request_id,
        message_id=payload.message_id,
    )
    return JSONResponse(
        status_code=result.status_code,
        content=result.body,
    )


def _feedback_error(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    body = chat_error(
        code,
        message,
        retryable=retryable,
        details=None,
    ).model_dump(mode="json")
    return JSONResponse(status_code=status_code, content=body)
