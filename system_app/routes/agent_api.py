from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.schemas import (
    AgentNotificationRequest,
    DoseTakenToolRequest,
    DoseTakenToolResult,
    MedicationDoseEventView,
    MedicationDoseStatusResult,
    NutritionDailySummaryResult,
    NutritionFoodDeleteRequest,
    NutritionFoodDeleteResult,
    NutritionFoodSearchRequest,
    NutritionFoodSearchResult,
    NutritionFoodUpdateRequest,
    NutritionFoodUpdateResult,
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
    SideEffectHistoryResult,
    SideEffectRecordRequest,
    SideEffectRecordResult,
    SystemPolicyApplyRequest,
)
from shared.settings import get_settings
from system_app.db import get_session
from system_app.routes.public_errors import public_error_code
from system_app.runtime import SystemRuntime
from system_app.security import require_internal_api_token
from system_app.models import DoseEvent
from system_app.services.agent_callback_service import (
    apply_agent_dose_taken_request,
    process_agent_notification_callback,
)
from system_app.services.nutrition_service import (
    daily_nutrition_view,
    delete_food,
    delete_meal,
    meal_view,
    meals_for_date,
    record_meal,
    search_foods,
    update_food,
    update_meal,
)
from system_app.services.nutrition_preference_service import nutrition_preference_summary, record_preference_fact
from system_app.services.policy_service import reload_policy_workbook
from system_app.services.clock_service import ensure_clock
from system_app.services.side_effect_record_service import list_side_effect_history, record_side_effect, side_effect_record_view

AGENT_NUTRITION_ERROR_CODES = {
    "food_name_required",
    "foods_required",
    "invalid_nutrient_value",
    "invalid_nutrition_date",
    "negative_nutrient_value",
    "nutrition_meal_not_found",
    "nutrition_meal_update_empty",
    "nutrition_food_not_found",
    "nutrition_food_update_empty",
    "unsupported_meal_type",
}
MEDICATION_DOSE_STATUS_RANGE_MAX_DAYS = 31
SIDE_EFFECT_HISTORY_RANGE_MAX_DAYS = 366
DoseStatus = {"scheduled", "taken", "missed"}
SideEffectSeverity = {"none", "low", "moderate", "high"}


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

    @router.get("/api/agent/dose-events", response_model=MedicationDoseStatusResult)
    async def agent_medication_dose_status(
        target_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        patient_id: str | None = None,
        status: str | None = None,
        medication_name: str | None = None,
        session: Session = Depends(get_session),
    ) -> MedicationDoseStatusResult:
        with get_runtime().write_lock:
            range_start, range_end, resolved_target_date = _resolve_query_date_range(
                target_date=target_date,
                start_date=start_date,
                end_date=end_date,
                default_date=ensure_clock(session).current_time.date(),
                max_days=MEDICATION_DOSE_STATUS_RANGE_MAX_DAYS,
                error_prefix="dose_status",
            )
            resolved_patient_id = patient_id or get_settings().patient_id
            if status and status not in DoseStatus:
                raise HTTPException(status_code=422, detail="invalid_dose_status_filter")
            stmt = (
                select(DoseEvent)
                .where(
                    DoseEvent.patient_id == resolved_patient_id,
                    DoseEvent.scheduled_for >= datetime.combine(range_start, time.min),
                    DoseEvent.scheduled_for <= datetime.combine(range_end, time.max),
                )
                .order_by(DoseEvent.scheduled_for.asc(), DoseEvent.medication_name.asc(), DoseEvent.id.asc())
            )
            if status:
                stmt = stmt.where(DoseEvent.status == status)
            if medication_name:
                stmt = stmt.where(DoseEvent.medication_name == medication_name)
            events = list(session.scalars(stmt).all())
            result = MedicationDoseStatusResult(
                success=True,
                patient_id=resolved_patient_id,
                target_date=resolved_target_date,
                start_date=range_start,
                end_date=range_end,
                dose_events=[
                    _dose_event_view(event) for event in events
                ],
                total=len(events),
                summary_by_date=_dose_summary_by_date(events, range_start, range_end),
                totals_by_status=_dose_status_totals(events),
            )
            session.commit()
            return result

    @router.post("/api/agent/side-effects/records", response_model=SideEffectRecordResult)
    async def agent_side_effect_record(payload: SideEffectRecordRequest, session: Session = Depends(get_session)) -> SideEffectRecordResult:
        with get_runtime().write_lock:
            record = record_side_effect(session, payload)
            result = SideEffectRecordResult.model_validate({"success": True, "record": side_effect_record_view(record)})
            session.commit()
            return result

    @router.get("/api/agent/side-effects/history", response_model=SideEffectHistoryResult)
    async def agent_side_effect_history(
        patient_id: str | None = None,
        limit: int = 20,
        suspected: bool | None = None,
        medication_name: str | None = None,
        severity: str | None = None,
        target_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        session: Session = Depends(get_session),
    ) -> SideEffectHistoryResult:
        range_start, range_end, resolved_target_date = _resolve_query_date_range(
            target_date=target_date,
            start_date=start_date,
            end_date=end_date,
            default_date=None,
            max_days=SIDE_EFFECT_HISTORY_RANGE_MAX_DAYS,
            error_prefix="side_effect_history",
        )
        if severity and severity not in SideEffectSeverity:
            raise HTTPException(status_code=422, detail="invalid_side_effect_history_severity")
        records = list_side_effect_history(
            session,
            patient_id=patient_id,
            limit=limit,
            suspected=suspected,
            medication_name=medication_name,
            severity=severity,
            start_date=range_start,
            end_date=range_end,
        )
        return SideEffectHistoryResult(
            success=True,
            target_date=resolved_target_date,
            start_date=range_start,
            end_date=range_end,
            records=[side_effect_record_view(record) for record in records],
            total=len(records),
        )

    @router.post("/api/agent/nutrition/food/search", response_model=NutritionFoodSearchResult)
    async def agent_nutrition_food_search(
        payload: NutritionFoodSearchRequest,
        session: Session = Depends(get_session),
    ) -> NutritionFoodSearchResult:
        result = search_foods(payload.query, limit=payload.limit, session=session, patient_id=payload.patient_id)
        result["query"] = payload.query
        result["meal_type"] = payload.meal_type or ""
        result["limit"] = payload.limit
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

    @router.post("/api/agent/nutrition/meals/{meal_id}/foods/{food_id}/update", response_model=NutritionFoodUpdateResult)
    async def agent_nutrition_update_food(
        meal_id: int,
        food_id: int,
        payload: NutritionFoodUpdateRequest,
        session: Session = Depends(get_session),
    ) -> NutritionFoodUpdateResult:
        with get_runtime().write_lock:
            try:
                result = update_food(
                    session,
                    meal_id=meal_id,
                    food_id=food_id,
                    patient_id=payload.patient_id,
                    food_ref_id=payload.food_ref_id,
                    food_name=payload.food_name,
                    portion=payload.portion,
                    nutrients=payload.nutrients,
                    reason=payload.reason,
                )
            except ValueError as exc:
                code = public_error_code(exc, allowed_codes=AGENT_NUTRITION_ERROR_CODES, fallback="nutrition_food_invalid")
                raise HTTPException(status_code=404 if code in {"nutrition_meal_not_found", "nutrition_food_not_found"} else 422, detail=code) from exc
            session.commit()
            return NutritionFoodUpdateResult.model_validate(result)

    @router.post("/api/agent/nutrition/meals/{meal_id}/foods/{food_id}/delete", response_model=NutritionFoodDeleteResult)
    async def agent_nutrition_delete_food(
        meal_id: int,
        food_id: int,
        payload: NutritionFoodDeleteRequest,
        session: Session = Depends(get_session),
    ) -> NutritionFoodDeleteResult:
        with get_runtime().write_lock:
            try:
                result = delete_food(
                    session,
                    meal_id=meal_id,
                    food_id=food_id,
                    patient_id=payload.patient_id,
                    reason=payload.reason,
                    delete_empty_meal=payload.delete_empty_meal,
                )
            except ValueError as exc:
                code = public_error_code(exc, allowed_codes=AGENT_NUTRITION_ERROR_CODES, fallback="nutrition_food_invalid")
                raise HTTPException(status_code=404 if code in {"nutrition_meal_not_found", "nutrition_food_not_found"} else 422, detail=code) from exc
            session.commit()
            return NutritionFoodDeleteResult.model_validate(result)

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
                randomize=payload.randomize,
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


def _resolve_query_date_range(
    *,
    target_date: str | None,
    start_date: str | None,
    end_date: str | None,
    default_date: date | None,
    max_days: int,
    error_prefix: str,
) -> tuple[date | None, date | None, date | None]:
    target_text = str(target_date or "").strip()
    start_text = str(start_date or "").strip()
    end_text = str(end_date or "").strip()
    if target_text and (start_text or end_text):
        raise HTTPException(status_code=422, detail=f"ambiguous_{error_prefix}_date_filter")
    if target_text:
        resolved = _parse_query_date(target_text, error_prefix)
        return resolved, resolved, resolved
    if start_text or end_text:
        if not start_text or not end_text:
            raise HTTPException(status_code=422, detail=f"{error_prefix}_date_range_required")
        range_start = _parse_query_date(start_text, error_prefix)
        range_end = _parse_query_date(end_text, error_prefix)
        if range_end < range_start:
            raise HTTPException(status_code=422, detail=f"invalid_{error_prefix}_date_range")
        if (range_end - range_start).days + 1 > max_days:
            raise HTTPException(status_code=422, detail=f"{error_prefix}_date_range_too_large")
        return range_start, range_end, None
    if default_date is None:
        return None, None, None
    return default_date, default_date, default_date


def _parse_query_date(value: str, error_prefix: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{error_prefix}_date") from exc


def _dose_event_view(event: DoseEvent) -> MedicationDoseEventView:
    return MedicationDoseEventView(
        dose_event_id=event.id,
        patient_id=event.patient_id,
        medication_name=event.medication_name,
        slot_label=event.slot_label,
        scheduled_for=event.scheduled_for,
        status=event.status,
        taken_at=event.taken_at,
        note=event.note,
    )


def _dose_status_totals(events: list[DoseEvent]) -> dict[str, int]:
    totals = {"scheduled": 0, "taken": 0, "missed": 0}
    for event in events:
        totals[event.status] = totals.get(event.status, 0) + 1
    return totals


def _dose_summary_by_date(events: list[DoseEvent], start_date: date, end_date: date) -> list[dict[str, int | str]]:
    summary: dict[date, dict[str, int | str]] = {}
    cursor = start_date
    while cursor <= end_date:
        summary[cursor] = {"date": cursor.isoformat(), "total": 0, "scheduled": 0, "taken": 0, "missed": 0}
        cursor = date.fromordinal(cursor.toordinal() + 1)
    for event in events:
        event_date = event.scheduled_for.date()
        row = summary.setdefault(event_date, {"date": event_date.isoformat(), "total": 0, "scheduled": 0, "taken": 0, "missed": 0})
        row["total"] = int(row.get("total", 0)) + 1
        row[event.status] = int(row.get(event.status, 0)) + 1
    return [summary[key] for key in sorted(summary)]
