from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncFailureRequest,
    AgentAsyncJobResultRequest,
    AgentAsyncPolicyChangeRequest,
    AgentAsyncPushMessageRequest,
)
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.security import require_internal_api_token
from system_app.services.agent_async_callback_service import (
    process_async_chat_result_callback,
    process_async_clinician_alert_callback,
    process_async_failure_callback,
    process_async_job_result_callback,
    process_async_policy_change_callback,
    process_async_push_message_callback,
)


def create_agent_async_api_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter(prefix="/api/agent/async", dependencies=[Depends(require_internal_api_token)])

    @router.post("/job-results")
    async def agent_async_job_results(payload: AgentAsyncJobResultRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_job_result_callback(session, payload)

    @router.post("/chat-results")
    async def agent_async_chat_results(payload: AgentAsyncChatResultRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_chat_result_callback(session, payload)

    @router.post("/policy-change-requests")
    async def agent_async_policy_change_requests(payload: AgentAsyncPolicyChangeRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_policy_change_callback(session, payload)

    @router.post("/push-messages")
    async def agent_async_push_messages(payload: AgentAsyncPushMessageRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_push_message_callback(session, payload)

    @router.post("/clinician-alerts")
    async def agent_async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_clinician_alert_callback(session, payload)

    @router.post("/failures")
    async def agent_async_failures(payload: AgentAsyncFailureRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_failure_callback(session, payload)

    return router
