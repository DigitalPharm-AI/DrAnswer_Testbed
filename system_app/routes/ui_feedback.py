from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from system_app.contracts_ui_feedback import (
    AgentChatFeedbackAccepted,
    UiChatOpinionAccepted,
    UiChatOpinionAcceptedResponse,
    UiChatOpinionRequest,
    UiFeedbackError,
    UiFeedbackErrorResponse,
)
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.services.agent_client import AgentServiceError
from system_app.services.ui_feedback_service import (
    UiFeedbackTargetNotFound,
    build_agent_feedback_payload,
    mark_feedback_submitted,
    resolve_feedback_target,
)

UI_CHAT_FEEDBACK_PATH = "/api/ui/v1/chat/feedback"
UI_CHAT_FEEDBACK_RESPONSES = {
    422: {
        "model": UiFeedbackErrorResponse,
        "description": "The feedback request schema or required field is invalid.",
    },
    404: {
        "model": UiFeedbackErrorResponse,
        "description": "The assistant message does not belong to the configured test patient.",
    },
    409: {
        "model": UiFeedbackErrorResponse,
        "description": "The request_id was already used with another feedback body.",
    },
    502: {
        "model": UiFeedbackErrorResponse,
        "description": "The Agent feedback boundary returned an invalid response.",
    },
    503: {
        "model": UiFeedbackErrorResponse,
        "description": "The Agent feedback boundary is temporarily unavailable.",
    },
    500: {
        "model": UiFeedbackErrorResponse,
        "description": "The Backend could not complete the UI feedback request.",
    },
}


def create_ui_feedback_router(
    get_runtime: Callable[[], SystemRuntime],
) -> APIRouter:
    router = APIRouter()

    @router.post(
        UI_CHAT_FEEDBACK_PATH,
        response_model=UiChatOpinionAcceptedResponse,
        status_code=202,
        responses=UI_CHAT_FEEDBACK_RESPONSES,
    )
    async def submit_chat_opinion(
        payload: UiChatOpinionRequest,
        session: Session = Depends(get_session),
    ):
        try:
            target = resolve_feedback_target(
                session,
                assistant_message_id=payload.assistant_message_id,
            )
        except UiFeedbackTargetNotFound:
            return _error_response(
                status_code=404,
                code="ASSISTANT_MESSAGE_NOT_FOUND",
                message="The assistant message could not be found.",
                retryable=False,
            )

        runtime = get_runtime()
        try:
            result = await runtime.agent_client.send_chat_feedback(
                build_agent_feedback_payload(payload, target)
            )
        except AgentServiceError as exc:
            if exc.error_type == "IDEMPOTENCY_CONFLICT":
                return _error_response(
                    status_code=409,
                    code="IDEMPOTENCY_CONFLICT",
                    message="The request_id was already used with another feedback body.",
                    retryable=False,
                )
            if exc.retryable or exc.status_code in {503, 504}:
                return _error_response(
                    status_code=503,
                    code="AGENT_FEEDBACK_UNAVAILABLE",
                    message="Feedback submission is temporarily unavailable.",
                    retryable=True,
                )
            return _error_response(
                status_code=502,
                code="AGENT_FEEDBACK_REJECTED",
                message="The feedback could not be accepted by the Agent.",
                retryable=False,
            )

        try:
            accepted = AgentChatFeedbackAccepted.model_validate(result)
        except ValidationError:
            return _error_response(
                status_code=502,
                code="AGENT_FEEDBACK_RESPONSE_INVALID",
                message="The Agent returned an invalid feedback response.",
                retryable=False,
            )
        if payload.reaction is not None and (
            accepted.reaction != payload.reaction
            or accepted.accepted_at is None
        ):
            return _error_response(
                status_code=502,
                code="AGENT_FEEDBACK_RESPONSE_INVALID",
                message="The Agent returned an invalid reaction response.",
                retryable=False,
            )
        try:
            with runtime.write_lock:
                feedback_status = mark_feedback_submitted(
                    session,
                    assistant_message_id=target.message_id,
                    request_id=payload.request_id,
                    requested_reaction=payload.reaction,
                    opinion_included=payload.opinion_text is not None,
                    agent_accepted_at=accepted.accepted_at,
                )
                session.commit()
        except Exception:
            session.rollback()
            return _error_response(
                status_code=503,
                code="FEEDBACK_STATUS_PERSIST_FAILED",
                message="The accepted feedback status could not be saved.",
                retryable=True,
            )
        return UiChatOpinionAcceptedResponse(
            success=True,
            data=UiChatOpinionAccepted(
                status="accepted",
                request_id=payload.request_id,
                assistant_message_id=target.message_id,
                reaction=feedback_status["reaction"],
                opinion_submitted=feedback_status[
                    "opinion_submitted"
                ],
                opinion_submitted_at=feedback_status[
                    "opinion_submitted_at"
                ],
            ),
            error=None,
        )

    return router


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
) -> JSONResponse:
    body = UiFeedbackErrorResponse(
        success=False,
        data=None,
        error=UiFeedbackError(
            code=code,
            message=message,
            retryable=retryable,
            details=None,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )
