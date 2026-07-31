from __future__ import annotations

from collections.abc import Callable
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from shared.backend_v13_contracts import (
    CommonErrorResponse,
    ContractError,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)
from shared.contract_errors import contract_error_definition
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.security import require_backend_api_bearer_token
from system_app.services.backend_v13_service import (
    POLICY_CHANGE_PATH,
    RECORD_CHANGE_PATH,
    BackendRequestConflict,
    BackendRequestGate,
    apply_notification_policy_change,
    apply_record_change,
)

RECORD_CHANGE_RESPONSES = {
    400: {"model": CommonErrorResponse, "description": "Request schema validation failed."},
    401: {"model": CommonErrorResponse, "description": "Bearer authentication failed."},
    404: {"model": CommonErrorResponse, "description": "The target record was not found."},
    409: {
        "model": CommonErrorResponse,
        "description": "Idempotency, confirmation reference, or version conflict.",
    },
    422: {"model": CommonErrorResponse, "description": "Business validation failed."},
    500: {"model": CommonErrorResponse, "description": "Unexpected Backend processing error."},
}
POLICY_CHANGE_RESPONSES = {
    400: {"model": CommonErrorResponse, "description": "Request schema validation failed."},
    401: {"model": CommonErrorResponse, "description": "Bearer authentication failed."},
    404: {"model": CommonErrorResponse, "description": "The public policy_id was not found."},
    409: {
        "model": CommonErrorResponse,
        "description": "Idempotency, confirmation reference, or version conflict.",
    },
    422: {
        "model": CommonErrorResponse,
        "description": "Date or policy boundary business validation failed.",
    },
    500: {
        "model": CommonErrorResponse,
        "description": "Unexpected Backend processing error.",
    },
}


def create_backend_v13_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post(
        RECORD_CHANGE_PATH,
        response_model=RecordChangeResponse,
        responses=RECORD_CHANGE_RESPONSES,
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
        responses=POLICY_CHANGE_RESPONSES,
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
            status_code = _contract_status(response)
            body = response.model_dump(mode="json")
            gate.complete(
                session,
                api_path=RECORD_CHANGE_PATH,
                request_id=payload.request_id,
                status_code=status_code,
                body=body,
                error_code=(
                    response.error.code
                    if isinstance(response, CommonErrorResponse)
                    else ""
                ),
            )
            session.commit()
            return JSONResponse(status_code=status_code, content=body)
        except BackendRequestConflict as exc:
            session.rollback()
            response = _contract_error(payload.request_id, exc.code, retryable=exc.code == "REQUEST_IN_PROGRESS")
            return JSONResponse(status_code=409, content=response.model_dump(mode="json"))
        except Exception:
            session.rollback()
            response = _contract_error(payload.request_id, "BACKEND_PROCESSING_ERROR", retryable=True)
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
            status_code = _contract_status(response)
            body = response.model_dump(mode="json")
            gate.complete(
                session,
                api_path=POLICY_CHANGE_PATH,
                request_id=payload.request_id,
                status_code=status_code,
                body=body,
                error_code=(
                    response.error.code
                    if isinstance(response, CommonErrorResponse)
                    else ""
                ),
            )
            session.commit()
            return JSONResponse(status_code=status_code, content=body)
        except BackendRequestConflict as exc:
            session.rollback()
            response = _contract_error(payload.request_id, exc.code, retryable=exc.code == "REQUEST_IN_PROGRESS")
            return JSONResponse(status_code=409, content=response.model_dump(mode="json"))
        except Exception:
            session.rollback()
            response = _contract_error(payload.request_id, "BACKEND_PROCESSING_ERROR", retryable=True)
            return JSONResponse(status_code=500, content=response.model_dump(mode="json"))


def _contract_status(
    response: (
        RecordChangeResponse
        | NotificationPolicyChangeResponse
        | CommonErrorResponse
    ),
) -> int:
    if not isinstance(response, CommonErrorResponse):
        return 200
    return contract_error_definition(response.error.code).status_code


def _contract_error(
    request_id: str,
    code: str,
    *,
    retryable: bool,
) -> CommonErrorResponse:
    definition = contract_error_definition(code)
    return CommonErrorResponse(
        request_id=request_id,
        error=ContractError(
            code=code,
            message=definition.message,
            retryable=retryable,
            details=None,
        ),
    )
