from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.agent_jobs import retry_agent_job
from system_app.services.conversation_service import acknowledge_notification as acknowledge_notification_record


def acknowledge_notification_response(
    runtime: SystemRuntime,
    notification_id: int,
    session: Session,
    *,
    resume_conversation_clock: bool = False,
) -> Response:
    with runtime.write_lock:
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
        with runtime.write_lock:
            job = retry_agent_job(session, job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="agent_job_not_found")
            if notification_id is not None:
                acknowledge_notification_record(session, notification_id, resume_conversation_clock=True, commit=False)
            session.commit()
        return hx_refresh()

    return router
