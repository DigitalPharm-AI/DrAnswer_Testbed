from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.services.health import collect_system_health


def create_health_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/health/details")
    async def health_details(session: Session = Depends(get_session)) -> dict:
        return await collect_system_health(session)

    return router
