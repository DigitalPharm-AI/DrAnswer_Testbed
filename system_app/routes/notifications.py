from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from system_app.db import get_session
from system_app.models import AgentJob, Notification
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.agent_jobs import (
    FAILED,
    RETRY_REQUESTED,
    complete_agent_job_retry_request,
    mark_agent_job_retry_requested,
    restore_agent_job_retry_failure,
    retry_agent_job,
)
from system_app.services.conversation_service import acknowledge_notification as acknowledge_notification_record
from system_app.services.observability_actions import post_agent_task_action


def acknowledge_notification_response(
    runtime: SystemRuntime,
    notification_id: int,
    session: Session,
    *,
    resume_conversation_clock: bool = False,
) -> Response:
    with runtime.write_lock:
        if session.get(Notification, notification_id) is None:
            raise HTTPException(status_code=404, detail="notification_not_found")
        acknowledge_notification_record(session, notification_id, resume_conversation_clock=resume_conversation_clock)
    return hx_refresh()


def create_notifications_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post("/notifications/{notification_id}/ack")
    async def acknowledge_notification(notification_id: int, session: Session = Depends(get_session)):
        return acknowledge_notification_response(get_runtime(), notification_id, session)

    @router.post("/agent-jobs/{job_id}/retry")
    async def retry_agent_job_request(
        job_id: int,
        notification_id: int | None = Query(None),
        session: Session = Depends(get_session),
    ):
        runtime = get_runtime()
        request_id = ""
        should_retry_agent_task = False
        with runtime.write_lock:
            job = session.get(AgentJob, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="agent_job_not_found")
            if notification_id is not None:
                notification = session.get(Notification, notification_id)
                if notification is None:
                    raise HTTPException(status_code=404, detail="notification_not_found")
                metadata = parse_json_object(notification.metadata_json)
                if metadata.get("agent_job_id") != job_id:
                    raise HTTPException(status_code=409, detail="notification_agent_job_mismatch")
                request_id = str(metadata.get("request_id") or metadata.get("agent_async_request_id") or "")

            if job.status != FAILED:
                detail = "agent_job_retry_in_progress" if job.status == RETRY_REQUESTED else "agent_job_retry_stale"
                raise HTTPException(status_code=409, detail=detail)

            should_retry_agent_task = bool(request_id)
            if should_retry_agent_task:
                mark_agent_job_retry_requested(session, job_id)
            else:
                retry_agent_job(session, job_id)
                if notification_id is not None:
                    acknowledge_notification_record(session, notification_id, resume_conversation_clock=True, commit=False)
            session.commit()

        if should_retry_agent_task:
            try:
                action_result = await post_agent_task_action(request_id, "retry", "retry from agent error notification")
                remote_retry_succeeded = action_result.get("success") is True
            except Exception:
                remote_retry_succeeded = False

            with runtime.write_lock:
                session.expire_all()
                job = session.get(AgentJob, job_id)
                if job is None:
                    raise HTTPException(status_code=404, detail="agent_job_not_found")
                if job.status != RETRY_REQUESTED:
                    raise HTTPException(status_code=409, detail="agent_job_retry_stale")
                if remote_retry_succeeded:
                    complete_agent_job_retry_request(session, job_id)
                    if notification_id is not None and session.get(Notification, notification_id) is not None:
                        acknowledge_notification_record(session, notification_id, resume_conversation_clock=True, commit=False)
                else:
                    restore_agent_job_retry_failure(session, job_id)
                session.commit()

            if not remote_retry_succeeded:
                raise HTTPException(status_code=502, detail="agent_task_retry_failed")
        return hx_refresh()

    return router
