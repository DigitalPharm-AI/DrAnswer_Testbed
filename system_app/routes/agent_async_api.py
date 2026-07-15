from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.redaction import stable_hash
from shared.schemas import (
    AgentAsyncChatResultRequest,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncFailureRequest,
    AgentAsyncJobResultRequest,
    AgentAsyncPolicyChangeRequest,
    AgentAsyncPushMessageRequest,
)
from system_app.db import get_session
from system_app.models import AgentRunStep, AgentRunTrace
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
from system_app.services.agent_trace_store import agent_trace_payload


def create_agent_async_api_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter(prefix="/api/agent/async", dependencies=[Depends(require_internal_api_token)])

    @router.get("/traces")
    def agent_async_traces(
        trace_id: str | None = None,
        request_id: str | None = None,
        patient_id: str | None = None,
        workflow_name: str | None = None,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        session: Session = Depends(get_session),
    ) -> dict:
        query = select(AgentRunTrace)
        if trace_id:
            query = query.where(AgentRunTrace.trace_id == trace_id)
        if request_id:
            query = query.where(AgentRunTrace.request_id == request_id)
        if patient_id:
            query = query.where(AgentRunTrace.patient_id_hash == stable_hash(patient_id))
        if workflow_name:
            query = query.where(AgentRunTrace.workflow_name == workflow_name)
        if status:
            query = query.where(AgentRunTrace.status == status)
        rows = session.scalars(query.order_by(desc(AgentRunTrace.updated_at), desc(AgentRunTrace.id)).limit(limit)).all()
        return {"status": "ok", "count": len(rows), "traces": [agent_trace_payload(row) for row in rows]}

    @router.get("/traces/{trace_id}")
    def agent_async_trace_detail(trace_id: str, session: Session = Depends(get_session)) -> dict:
        trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == trace_id))
        if trace is None:
            raise HTTPException(status_code=404, detail="agent_trace_not_found")
        steps = list(
            session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id == trace_id).order_by(AgentRunStep.id.asc())).all()
        )
        return {"status": "ok", "trace": agent_trace_payload(trace, steps)}

    @router.post("/job-results")
    def agent_async_job_results(payload: AgentAsyncJobResultRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_job_result_callback(session, payload)

    @router.post("/chat-results")
    def agent_async_chat_results(payload: AgentAsyncChatResultRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_chat_result_callback(session, payload)

    @router.post("/policy-change-requests")
    def agent_async_policy_change_requests(payload: AgentAsyncPolicyChangeRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_policy_change_callback(session, payload)

    @router.post("/push-messages")
    def agent_async_push_messages(payload: AgentAsyncPushMessageRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_push_message_callback(session, payload)

    @router.post("/clinician-alerts")
    def agent_async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_clinician_alert_callback(session, payload)

    @router.post("/failures")
    def agent_async_failures(payload: AgentAsyncFailureRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_async_failure_callback(session, payload)

    return router
