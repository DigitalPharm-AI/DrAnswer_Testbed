from __future__ import annotations

from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.medication_plan_service import create_medication_plan, delete_medication_plan
from system_app.services.patient_profile_service import apply_phr_registration_result, build_phr_registration_items, get_patient_profile, mark_phr_sync_failed
from system_app.services.phr_client import PhrServiceError
from system_app.services.simulation_constants import CUSTOM_CHOICE, resolve_choice_value, resolve_schedule_times


def create_medications_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()

    @router.post("/medications")
    async def add_medication(
        medication_name: str = Form(""),
        medication_choice: str = Form(CUSTOM_CHOICE),
        medication_name_custom: str = Form(""),
        dosage: str = Form(""),
        dosage_choice: str = Form(CUSTOM_CHOICE),
        dosage_custom: str = Form(""),
        start_date: str = Form(...),
        end_date: str = Form(...),
        times_csv: str = Form(""),
        schedule_template: str = Form("morning_lunch_evening"),
        instructions: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            effective_medication_name = resolve_choice_value(medication_choice, medication_name_custom, medication_name)
            has_dosage_value = (dosage_choice and dosage_choice != CUSTOM_CHOICE) or (dosage_custom and dosage_custom.strip()) or (dosage and dosage.strip())
            effective_dosage = resolve_choice_value(dosage_choice, dosage_custom, dosage) if has_dosage_value else ""
            effective_times_csv = resolve_schedule_times(schedule_template, times_csv)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        with get_runtime().write_lock:
            create_medication_plan(
                session,
                medication_name=effective_medication_name,
                dosage=effective_dosage,
                start_date=date.fromisoformat(start_date),
                end_date=date.fromisoformat(end_date),
                times_csv=effective_times_csv,
                instructions=instructions,
            )
        return hx_refresh()

    @router.post("/medications/{plan_id}/delete")
    async def remove_medication(plan_id: int, session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            delete_medication_plan(session, plan_id)
        return hx_refresh()

    @router.post("/phr/register")
    async def register_phr_patient(session: Session = Depends(get_session)):
        runtime = get_runtime()
        with runtime.write_lock:
            medications = build_phr_registration_items(session)
            profile = get_patient_profile(session)
            existing_key = profile.phr_patient_key if profile is not None and profile.phr_patient_key else ""
            session.commit()

        try:
            if existing_key:
                result = await runtime.phr_client.update_patient_medications(existing_key, medications)
            else:
                result = await runtime.phr_client.register_patient(medications)
        except PhrServiceError as exc:
            with runtime.write_lock:
                mark_phr_sync_failed(session, str(exc))
                session.commit()
            return hx_refresh()

        with runtime.write_lock:
            apply_phr_registration_result(session, result)
            session.commit()
        return hx_refresh()

    return router
