from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent_app.integration.contracts import (
    ContractError,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)
from system_app.contracts_v12 import BackendChatFailure, BackendChatRequest, BackendChatResponse
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.security import require_backend_api_bearer_token
from system_app.services.agent_client import AgentServiceError
from system_app.services.backend_chat_service import (
    BackendChatConflict,
    build_agent_chat_request,
    call_agent_and_persist_response,
    existing_backend_chat_response,
    mark_user_message_failed,
    persist_user_message,
)
from system_app.services.backend_v12_service import (
    POLICY_CHANGE_PATH,
    RECORD_CHANGE_PATH,
    BackendRequestConflict,
    BackendRequestGate,
    apply_notification_policy_change,
    apply_record_change,
)


def create_backend_v12_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post("/api/chat/sync", response_model=BackendChatResponse)
    async def backend_sync_chat(
        payload: BackendChatRequest,
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        try:
            with runtime.write_lock:
                user_message, request_id, message_at = persist_user_message(session, payload)
                replay = existing_backend_chat_response(session, request_id=request_id)
                agent_request = build_agent_chat_request(
                    user_message=user_message,
                    request_id=request_id,
                    message_at=message_at,
                    requested_return_type=payload.requested_return_type,
                )
                session.commit()
                if replay is not None:
                    return replay
                user_message_id = user_message.id
        except BackendChatConflict:
            session.rollback()
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": "IDEMPOTENCY_CONFLICT",
                        "message": "The request_id was already used with a different chat body.",
                        "retryable": False,
                        "details": None,
                    }
                },
            )

        try:
            response = await call_agent_and_persist_response(
                session,
                runtime.agent_client,
                request=agent_request,
            )
            with runtime.write_lock:
                session.commit()
            return response
        except AgentServiceError as exc:
            session.rollback()
            with runtime.write_lock:
                mark_user_message_failed(
                    session,
                    user_message_id=user_message_id,
                    error_code=exc.error_type,
                    retryable=exc.retryable,
                )
                session.commit()
            failure = BackendChatFailure(
                request_id=request_id,
                conversation_id=payload.conversation_id,
                user_message_id=str(user_message_id),
                error_code=exc.error_type,
                retryable=exc.retryable,
            )
            return JSONResponse(status_code=502, content=failure.model_dump(mode="json"))
        except Exception:
            session.rollback()
            with runtime.write_lock:
                mark_user_message_failed(
                    session,
                    user_message_id=user_message_id,
                    error_code="BACKEND_CHAT_PROCESSING_ERROR",
                    retryable=True,
                )
                session.commit()
            failure = BackendChatFailure(
                request_id=request_id,
                conversation_id=payload.conversation_id,
                user_message_id=str(user_message_id),
                error_code="BACKEND_CHAT_PROCESSING_ERROR",
                retryable=True,
            )
            return JSONResponse(status_code=500, content=failure.model_dump(mode="json"))

    @router.post(
        RECORD_CHANGE_PATH,
        response_model=RecordChangeResponse,
        dependencies=[Depends(require_backend_api_bearer_token)],
    )
    async def backend_record_change(
        payload: RecordChangeRequest,
        session: Session = Depends(get_session),
    ):
        return _process_record_change(get_runtime(), session, payload)

    @router.post(
        POLICY_CHANGE_PATH,
        response_model=NotificationPolicyChangeResponse,
        dependencies=[Depends(require_backend_api_bearer_token)],
    )
    async def backend_policy_change(
        payload: NotificationPolicyChangeRequest,
        session: Session = Depends(get_session),
    ):
        return _process_policy_change(get_runtime(), session, payload)

    return router


def _process_record_change(runtime: SystemRuntime, session: Session, payload: RecordChangeRequest):
    gate = BackendRequestGate()
    with runtime.write_lock:
        try:
            replay = gate.begin(
                session,
                api_path=RECORD_CHANGE_PATH,
                request_id=payload.request_id,
                payload=payload,
            )
            if replay is not None:
                session.rollback()
                return JSONResponse(status_code=replay.status_code, content=replay.body)
            response = apply_record_change(session, payload)
            status_code = _contract_status(response.success, response.error)
            body = response.model_dump(mode="json")
            gate.complete(
                session,
                api_path=RECORD_CHANGE_PATH,
                request_id=payload.request_id,
                status_code=status_code,
                body=body,
                error_code=response.error.code if response.error else "",
            )
            session.commit()
            return JSONResponse(status_code=status_code, content=body)
        except BackendRequestConflict as exc:
            session.rollback()
            response = _record_error(payload.request_id, exc.code, retryable=exc.code == "REQUEST_IN_PROGRESS")
            return JSONResponse(status_code=409, content=response.model_dump(mode="json"))
        except Exception:
            session.rollback()
            response = _record_error(payload.request_id, "BACKEND_PROCESSING_ERROR", retryable=True)
            return JSONResponse(status_code=500, content=response.model_dump(mode="json"))


def _process_policy_change(runtime: SystemRuntime, session: Session, payload: NotificationPolicyChangeRequest):
    gate = BackendRequestGate()
    with runtime.write_lock:
        try:
            replay = gate.begin(
                session,
                api_path=POLICY_CHANGE_PATH,
                request_id=payload.request_id,
                payload=payload,
            )
            if replay is not None:
                session.rollback()
                return JSONResponse(status_code=replay.status_code, content=replay.body)
            response = apply_notification_policy_change(session, payload)
            status_code = _contract_status(response.success, response.error)
            body = response.model_dump(mode="json")
            gate.complete(
                session,
                api_path=POLICY_CHANGE_PATH,
                request_id=payload.request_id,
                status_code=status_code,
                body=body,
                error_code=response.error.code if response.error else "",
            )
            session.commit()
            return JSONResponse(status_code=status_code, content=body)
        except BackendRequestConflict as exc:
            session.rollback()
            response = _policy_error(payload.request_id, exc.code, retryable=exc.code == "REQUEST_IN_PROGRESS")
            return JSONResponse(status_code=409, content=response.model_dump(mode="json"))
        except Exception:
            session.rollback()
            response = _policy_error(payload.request_id, "BACKEND_PROCESSING_ERROR", retryable=True)
            return JSONResponse(status_code=500, content=response.model_dump(mode="json"))


def _contract_status(success: bool, error: ContractError | None) -> int:
    if success:
        return 200
    code = error.code if error else ""
    if code in {"VERSION_CONFLICT", "CONFIRMATION_MESSAGE_NOT_FOUND", "SOURCE_CHAT_REQUEST_NOT_FOUND"}:
        return 409
    if code.endswith("_not_found") or code.endswith("_NOT_FOUND"):
        return 404
    return 422


def _record_error(request_id: str, code: str, *, retryable: bool) -> RecordChangeResponse:
    return RecordChangeResponse(
        success=False,
        request_id=request_id,
        result=None,
        error=ContractError(code=code, message=code, retryable=retryable, details=None),
        processed_at=datetime.now(UTC),
    )


def _policy_error(request_id: str, code: str, *, retryable: bool) -> NotificationPolicyChangeResponse:
    return NotificationPolicyChangeResponse(
        success=False,
        request_id=request_id,
        result=None,
        error=ContractError(code=code, message=code, retryable=retryable, details=None),
        processed_at=datetime.now(UTC),
    )
