from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.routes.public_errors import public_error_code
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.dashboard_view import build_dashboard_context
from system_app.services.nutrition_preference_service import record_preference_csv_lists
from system_app.services.nutrition_service import daily_nutrition_view, delete_food, run_nutrition_scenario

NUTRITION_SCENARIO_ERROR_CODES = {
    "unknown_nutrition_scenario",
}

NUTRITION_FOOD_ERROR_CODES = {
    "nutrition_meal_not_found",
    "nutrition_food_not_found",
}


def create_nutrition_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.get("/partials/nutrition", response_class=HTMLResponse)
    async def nutrition_partial(request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        return get_runtime().templates.TemplateResponse(request, "partials/nutrition.html", build_dashboard_context(request, session))

    @router.post("/nutrition/scenarios/{scenario_key}", response_class=HTMLResponse)
    async def nutrition_scenario(scenario_key: str, request: Request, session: Session = Depends(get_session)) -> HTMLResponse:
        runtime = get_runtime()
        with runtime.write_lock:
            try:
                run_nutrition_scenario(session, scenario_key)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail=public_error_code(exc, allowed_codes=NUTRITION_SCENARIO_ERROR_CODES, fallback="nutrition_scenario_invalid"),
                ) from exc
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/nutrition.html", build_dashboard_context(request, session))

    @router.post("/nutrition/preferences")
    async def nutrition_preferences(
        patient_id: str = Form(""),
        preferred_foods_csv: str = Form(""),
        avoided_foods_csv: str = Form(""),
        diet_styles_csv: str = Form(""),
        session: Session = Depends(get_session),
    ):
        with get_runtime().write_lock:
            record_preference_csv_lists(
                session,
                patient_id=patient_id or None,
                preferred_foods_csv=preferred_foods_csv,
                avoided_foods_csv=avoided_foods_csv,
                diet_styles_csv=diet_styles_csv,
            )
            session.commit()
        return hx_refresh()

    @router.post("/nutrition/meals/{meal_id}/foods/{food_id}/delete", response_class=HTMLResponse)
    async def nutrition_food_delete(
        meal_id: int,
        food_id: int,
        request: Request,
        patient_id: str = Form(""),
        session: Session = Depends(get_session),
    ) -> HTMLResponse:
        runtime = get_runtime()
        with runtime.write_lock:
            try:
                delete_food(session, meal_id=meal_id, food_id=food_id, patient_id=patient_id or None)
            except ValueError as exc:
                code = public_error_code(exc, allowed_codes=NUTRITION_FOOD_ERROR_CODES, fallback="nutrition_food_invalid")
                raise HTTPException(status_code=404, detail=code) from exc
            session.commit()
        return runtime.templates.TemplateResponse(request, "partials/nutrition.html", build_dashboard_context(request, session))

    @router.get("/api/nutrition/daily-summary")
    async def nutrition_daily_summary(patient_id: str | None = None, session: Session = Depends(get_session)) -> dict:
        return {"success": True, "daily_summary": daily_nutrition_view(session, patient_id=patient_id)}

    return router
