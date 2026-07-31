from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from shared.schemas import (
    AgentNotificationRequest,
)
from system_app.db import get_session
from system_app.runtime import SystemRuntime
from system_app.security import require_internal_api_token
from system_app.services.agent_callback_service import (
    process_agent_notification_callback,
)
from system_app.services.policy_service import reload_policy_workbook


def create_agent_api_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_internal_api_token)])

    @router.post("/api/agent/notifications")
    async def agent_notification_callback(payload: AgentNotificationRequest, session: Session = Depends(get_session)) -> dict:
        with get_runtime().write_lock:
            return process_agent_notification_callback(session, payload)

    @router.post("/admin/policies/reload")
    async def admin_reload_policies() -> dict:
        with get_runtime().write_lock:
            result = reload_policy_workbook()
        return result.model_dump(mode="json")

    return router
