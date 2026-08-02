from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from shared.async_v13_contracts import (
    AsyncResultCallbackAck,
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposalAck,
)
from shared.backend_v13_contracts import CommonErrorResponse, ContractError
from shared.contract_errors import contract_error_definition
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentAsyncPushMessageRequest,
)
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.security import (
    require_backend_api_bearer_token,
    require_internal_api_token,
)
from system_app.services.agent_async_callback_service import (
    AsyncCallbackContractError,
    process_async_clinician_alert_callback,
    process_async_push_message_callback,
    process_missed_dose_result_callback,
    process_notification_policy_change_proposal,
)


def create_agent_async_api_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter(prefix="/api/agent/async")

    @router.post(
        "/missed-dose-results",
        response_model=AsyncResultCallbackAck,
        dependencies=[Depends(require_backend_api_bearer_token)],
        responses={
            400: {"model": CommonErrorResponse},
            401: {"model": CommonErrorResponse},
            404: {"model": CommonErrorResponse},
            409: {"model": CommonErrorResponse},
            500: {"model": CommonErrorResponse},
        },
    )
    def agent_async_missed_dose_results(
        payload: MissedDoseResultCallback,
        session: Session = Depends(get_session),
    ) -> AsyncResultCallbackAck:
        try:
            with get_runtime().write_lock:
                return process_missed_dose_result_callback(
                    session,
                    payload,
                )
        except AsyncCallbackContractError as exc:
            session.rollback()
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.error.model_dump(mode="json"),
            ) from exc
        except HTTPException:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=ContractError(
                    code="BACKEND_PROCESSING_ERROR",
                    message=contract_error_definition(
                        "BACKEND_PROCESSING_ERROR"
                    ).message,
                    retryable=True,
                    details=None,
                ).model_dump(mode="json"),
            ) from exc

    @router.post(
        "/notification-policy-change-proposals",
        response_model=NotificationPolicyProposalAck,
        status_code=202,
        dependencies=[Depends(require_backend_api_bearer_token)],
        responses={
            200: {
                "model": NotificationPolicyProposalAck,
                "description": "The same proposal was already accepted.",
            },
            400: {"model": CommonErrorResponse},
            401: {"model": CommonErrorResponse},
            409: {"model": CommonErrorResponse},
            422: {"model": CommonErrorResponse},
            500: {"model": CommonErrorResponse},
        },
    )
    def agent_async_notification_policy_change_proposals(
        payload: NotificationPolicyChangeProposalRequest,
        response: Response,
        session: Session = Depends(get_session),
    ) -> NotificationPolicyProposalAck:
        try:
            with get_runtime().write_lock:
                ack, created = process_notification_policy_change_proposal(
                    session,
                    payload,
                )
            if not created:
                response.status_code = 200
            return ack
        except AsyncCallbackContractError as exc:
            session.rollback()
            raise HTTPException(
                status_code=exc.status_code,
                detail=exc.error.model_dump(mode="json"),
            ) from exc
        except HTTPException:
            session.rollback()
            raise
        except Exception as exc:
            session.rollback()
            raise HTTPException(
                status_code=500,
                detail=ContractError(
                    code="BACKEND_PROCESSING_ERROR",
                    message=contract_error_definition(
                        "BACKEND_PROCESSING_ERROR"
                    ).message,
                    retryable=True,
                    details=None,
                ).model_dump(mode="json"),
            ) from exc

    @router.post(
        "/push-messages",
        dependencies=[Depends(require_internal_api_token)],
        include_in_schema=False,
    )
    def agent_async_push_messages(payload: AgentAsyncPushMessageRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_push_message_callback(session, payload)

    @router.post(
        "/clinician-alerts",
        dependencies=[Depends(require_internal_api_token)],
        include_in_schema=False,
    )
    def agent_async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_clinician_alert_callback(session, payload)

    return router
