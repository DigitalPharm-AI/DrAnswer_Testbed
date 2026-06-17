from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.agent_response_service import (
    run_manual_pattern_analysis,
)
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import mark_dose_taken, set_clock_state
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.patient_profile_service import can_run_simulation
from system_app.services.side_effect_reminder_safety import (
    set_reminder_suppressed_after_side_effect,
)


def simulation_guard_allows_run(session: Session) -> bool:
    if can_run_simulation(session):
        return True
    clock = ensure_clock(session)
    clock.is_running = False
    clock.speed_multiplier = 0
    session.commit()
    return False


def create_simulation_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post("/clock/advance")
    async def advance_clock(minutes: int = Form(...), session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            if not simulation_guard_allows_run(session):
                return hx_refresh()
            await set_clock_state(session, advance_minutes=minutes)
        return hx_refresh()

    @router.post("/clock/play")
    async def play_clock(speed_multiplier: int = Form(...), session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            if not simulation_guard_allows_run(session):
                return hx_refresh()
            await set_clock_state(session, is_running=True, speed_multiplier=speed_multiplier)
        return hx_refresh()

    @router.post("/clock/pause")
    async def pause_clock(session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            await set_clock_state(session, is_running=False, speed_multiplier=0)
        return hx_refresh()

    @router.post("/analysis/run-today")
    async def analyze_today(session: Session = Depends(get_session)):
        runtime = get_runtime()
        with runtime.write_lock:
            if not simulation_guard_allows_run(session):
                return hx_refresh()
            await run_manual_pattern_analysis(session, runtime.agent_client)
        return hx_refresh()

    @router.post("/agent/model-tier")
    async def update_agent_model_tier(model_tier: str = Form(...)):
        await get_runtime().agent_client.set_model_tier(model_tier)
        return hx_refresh()

    @router.post("/reminders/suppression")
    async def update_reminder_suppression(suppressed: bool = Form(...), session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            set_reminder_suppressed_after_side_effect(
                session,
                suppressed,
                reason="사용자가 프론트 UI에서 복약/AI 알림 전체 상태를 변경했습니다.",
            )
            session.commit()
        return hx_refresh()

    @router.post("/simulation/reset")
    async def reset_simulation(session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            reset_simulation_state(session)
        return hx_refresh()

    @router.post("/doses/{dose_event_id}/take")
    async def take_dose(dose_event_id: int, session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            mark_dose_taken(session, dose_event_id)
        return hx_refresh()

    return router
