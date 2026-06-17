from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.models import Notification
from system_app.runtime import SystemRuntime
from system_app.services.clock_service import ensure_clock
from system_app.services.dashboard_view import build_dashboard_context, resolve_agent_model_config, serialize_notification_feed, serialize_notifications


def create_pages_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    async def index(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        runtime = get_runtime()
        return runtime.templates.TemplateResponse(
            request,
            "index.html",
            build_dashboard_context(request, session, agent_model_config=await resolve_agent_model_config(runtime.agent_client)),
        )

    @router.get("/partials/time-bar", response_class=HTMLResponse)
    async def time_bar_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/time_bar.html", build_dashboard_context(request, session))

    @router.get("/partials/timeline", response_class=HTMLResponse)
    async def timeline_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/timeline.html", build_dashboard_context(request, session))

    @router.get("/partials/notifications", response_class=HTMLResponse)
    async def notifications_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/notifications.html", build_dashboard_context(request, session))

    @router.get("/partials/active-policies", response_class=HTMLResponse)
    async def active_policies_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/active_policies.html", build_dashboard_context(request, session))

    @router.get("/partials/chat", response_class=HTMLResponse)
    async def chat_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/chat.html", build_dashboard_context(request, session))

    @router.get("/partials/chat-log", response_class=HTMLResponse)
    async def chat_log_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/chat_log.html", build_dashboard_context(request, session))

    @router.get("/partials/chat-history", response_class=HTMLResponse)
    async def chat_history_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/conversation_history.html", build_dashboard_context(request, session))

    @router.get("/partials/system-request-history", response_class=HTMLResponse)
    async def system_request_history_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/system_request_history.html", build_dashboard_context(request, session))

    @router.get("/api/notifications/feed")
    async def notifications_feed(after_id: int = 0, session: Session = Depends(get_session)) -> dict:
        clock = ensure_clock(session)
        notifications = serialize_notification_feed(session, clock.current_time, after_id=after_id)
        last_seen_id = after_id
        if notifications:
            last_seen_id = max(row["id"] for row in notifications)
        return {"notifications": notifications, "last_seen_id": last_seen_id, "current_time": clock.current_time.isoformat()}

    @router.get("/api/notifications/{notification_id}")
    async def notification_detail(notification_id: int, session: Session = Depends(get_session)) -> dict:
        notification = session.get(Notification, notification_id)
        if notification is None:
            raise HTTPException(status_code=404, detail="notification_not_found")
        return {"notification": serialize_notifications(session, [notification])[0]}

    return router
