from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from shared.schemas import (
    AgentNotificationRequest,
    DoseTakenToolRequest,
    DoseTakenToolResult,
    NutritionDailySummaryResult,
    NutritionFoodSearchRequest,
    NutritionFoodSearchResult,
    NutritionMealDeleteRequest,
    NutritionMealDeleteResult,
    NutritionMealListResult,
    NutritionMealRecordRequest,
    NutritionMealRecordResult,
    NutritionMealUpdateRequest,
    NutritionMealUpdateResult,
    NutritionPreferenceFactRequest,
    NutritionPreferenceFactResult,
    NutritionPreferenceSummaryResult,
    NutritionRecommendRequest,
    NutritionRecommendResult,
    PolicyApplyRequest,
    SystemPolicyApplyRequest,
)
from system_app.db import get_session
from system_app.routes.public_errors import public_error_code
from system_app.runtime import SystemRuntime
from system_app.security import require_internal_api_token
from system_app.services.agent_callback_service import (
    apply_agent_dose_taken_request,
    process_agent_notification_callback,
)
from system_app.services.nutrition_service import (
    daily_nutrition_view,
    delete_meal,
    meal_view,
    meals_for_date,
    record_meal,
    search_foods,
    update_meal,
)
from system_app.services.nutrition_preference_service import nutrition_preference_summary, record_preference_fact
from system_app.services.policy_service import reload_policy_workbook

AGENT_NUTRITION_ERROR_CODES = {
    "food_name_required",
    "foods_required",
    "invalid_nutrient_value",
    "invalid_nutrition_date",
    "negative_nutrient_value",
    "nutrition_meal_not_found",
    "nutrition_meal_update_empty",
    "unsupported_meal_type",
}


def create_agent_api_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_internal_api_token)])

    @router.post("/api/agent/policies/apply")
    async def agent_policy_apply(_: PolicyApplyRequest) -> None:
        raise HTTPException(status_code=410, detail="policy_apply_requires_confirmation")

    @router.post("/api/agent/system-policies/apply")
    async def agent_system_policy_apply(_: SystemPolicyApplyRequest) -> None:
        raise HTTPException(status_code=410, detail="system_policy_apply_requires_confirmation")

    @router.post("/api/agent/dose-events/mark-taken", response_model=DoseTakenToolResult)
    async def agent_dose_taken(payload: DoseTakenToolRequest, session: Session = Depends(get_session)) -> DoseTakenToolResult:
        with get_runtime().write_lock:
            return apply_agent_dose_taken_request(session, payload)

    @router.post("/api/agent/nutrition/food/search", response_model=NutritionFoodSearchResult)
    async def agent_nutrition_food_search(
        payload: NutritionFoodSearchRequest,
        session: Session = Depends(get_session),
    ) -> NutritionFoodSearchResult:
        result = search_foods(payload.query, limit=payload.limit, session=session, patient_id=payload.patient_id)
        result["query"] = payload.query
        return NutritionFoodSearchResult.model_validate(result)

    @router.post("/api/agent/nutrition/meals", response_model=NutritionMealRecordResult)
    async def agent_nutrition_record_meal(
        payload: NutritionMealRecordRequest,
        session: Session = Depends(get_session),
    ) -> NutritionMealRecordResult:
        with get_runtime().write_lock:
            try:
                result = record_meal(
                    session,
                    patient_id=payload.patient_id,
                    foods=[food.model_dump(mode="json") for food in payload.foods],
                    meal_type=payload.meal_type,
                    meal_date=payload.meal_date,
                    meal_time=payload.meal_time,
                    scenario_key=payload.scenario_key,
                    description=payload.description,
                )
            except ValueError as exc:
                raise HTTPException(
                    status_code=422,
                    detail=public_error_code(exc, allowed_codes=AGENT_NUTRITION_ERROR_CODES, fallback="nutrition_meal_invalid"),
                ) from exc
            session.commit()
            return NutritionMealRecordResult.model_validate(result)

    @router.post("/api/agent/nutrition/meals/{meal_id}/update", response_model=NutritionMealUpdateResult)
    async def agent_nutrition_update_meal(
        meal_id: int,
        payload: NutritionMealUpdateRequest,
        session: Session = Depends(get_session),
    ) -> NutritionMealUpdateResult:
        with get_runtime().write_lock:
            try:
                result = update_meal(
                    session,
                    meal_id=meal_id,
                    patient_id=payload.patient_id,
                    foods=[food.model_dump(mode="json") for food in payload.foods] if payload.foods is not None else None,
                    meal_type=payload.meal_type,
                    meal_date=payload.meal_date,
                    meal_time=payload.meal_time,
                    scenario_key=payload.scenario_key,
                    description=payload.description,
                    reason=payload.reason,
                )
            except ValueError as exc:
                code = public_error_code(exc, allowed_codes=AGENT_NUTRITION_ERROR_CODES, fallback="nutrition_meal_invalid")
                raise HTTPException(status_code=404 if code == "nutrition_meal_not_found" else 422, detail=code) from exc
            session.commit()
            return NutritionMealUpdateResult.model_validate(result)

    @router.post("/api/agent/nutrition/meals/{meal_id}/delete", response_model=NutritionMealDeleteResult)
    async def agent_nutrition_delete_meal(
        meal_id: int,
        payload: NutritionMealDeleteRequest,
        session: Session = Depends(get_session),
    ) -> NutritionMealDeleteResult:
        with get_runtime().write_lock:
            try:
                result = delete_meal(
                    session,
                    meal_id=meal_id,
                    patient_id=payload.patient_id,
                    reason=payload.reason,
                )
            except ValueError as exc:
                code = public_error_code(exc, allowed_codes=AGENT_NUTRITION_ERROR_CODES, fallback="nutrition_meal_invalid")
                raise HTTPException(status_code=404 if code == "nutrition_meal_not_found" else 422, detail=code) from exc
            session.commit()
            return NutritionMealDeleteResult.model_validate(result)

    @router.get("/api/agent/nutrition/meals", response_model=NutritionMealListResult)
    async def agent_nutrition_list_meals(
        meal_date: str | None = None,
        patient_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> NutritionMealListResult:
        with get_runtime().write_lock:
            summary = daily_nutrition_view(session, meal_date, patient_id=patient_id)
            target_date = summary["date"]
            meals = meals_for_date(session, summary["patient_id"], date.fromisoformat(target_date))
            result = NutritionMealListResult(success=True, meals=[meal_view(session, meal) for meal in meals], total=len(meals))
            session.commit()
            return result

    @router.get("/api/agent/nutrition/daily-summary", response_model=NutritionDailySummaryResult)
    async def agent_nutrition_daily_summary(
        meal_date: str | None = None,
        patient_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> NutritionDailySummaryResult:
        with get_runtime().write_lock:
            result = NutritionDailySummaryResult(success=True, daily_summary=daily_nutrition_view(session, meal_date, patient_id=patient_id))
            session.commit()
            return result

    @router.post("/api/agent/nutrition/preferences/facts", response_model=NutritionPreferenceFactResult)
    async def agent_nutrition_record_preference(
        payload: NutritionPreferenceFactRequest,
        session: Session = Depends(get_session),
    ) -> NutritionPreferenceFactResult:
        with get_runtime().write_lock:
            result = record_preference_fact(
                session,
                patient_id=payload.patient_id,
                predicate=payload.predicate,
                object_label=payload.object_label,
                object_type=payload.object_type,
                strength=payload.strength,
                safety_level=payload.safety_level,
                confidence=payload.confidence,
                source=payload.source,
                evidence_text=payload.evidence_text,
                source_trace_id=payload.source_trace_id,
            )
            session.commit()
            return NutritionPreferenceFactResult.model_validate(result)

    @router.get("/api/agent/nutrition/preferences", response_model=NutritionPreferenceSummaryResult)
    async def agent_nutrition_preferences(
        patient_id: str | None = None,
        session: Session = Depends(get_session),
    ) -> NutritionPreferenceSummaryResult:
        return NutritionPreferenceSummaryResult(success=True, preferences=nutrition_preference_summary(session, patient_id=patient_id))

    @router.post("/api/agent/nutrition/recommend", response_model=NutritionRecommendResult)
    async def agent_nutrition_recommend(
        payload: NutritionRecommendRequest,
        session: Session = Depends(get_session),
    ) -> NutritionRecommendResult:
        from system_app.services.diet_recommendation_service import recommend_diet as diet_recommend
        try:
            result = diet_recommend(
                session,
                patient_id=payload.patient_id,
                constraints=payload.constraints,
                meal_type=payload.meal_type,
                limit=payload.limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=public_error_code(exc, allowed_codes={"constraints_required", "invalid_constraint_level"}, fallback="diet_recommend_invalid")) from exc
        return NutritionRecommendResult.model_validate(result)

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
