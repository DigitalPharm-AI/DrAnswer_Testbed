from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException
from sqlalchemy.orm import Session

from system_app.db import get_session
from system_app.routes.public_errors import public_error_code, public_phr_sync_failure_message
from system_app.routes.responses import hx_refresh
from system_app.runtime import SystemRuntime
from system_app.services.medication_plan_service import (
    MedicationSubmissionConflictError,
    create_medication_plan,
    delete_medication_plan,
    normalize_medication_submission_id,
)
from system_app.services.patient_profile_service import (
    apply_phr_registration_result,
    build_phr_registration_items,
    get_patient_profile,
    mark_phr_sync_failed,
    phr_medication_snapshot_fingerprint,
)
from system_app.services.phr_client import PhrServiceError
from system_app.services.simulation_constants import CUSTOM_CHOICE, parse_times_csv, resolve_choice_value, resolve_schedule_times

MEDICATION_FORM_ERROR_CODES = {
    "invalid_date_format",
    "invalid_date_range",
    "invalid_schedule_time_format",
    "required_choice_missing",
    "required_schedule_missing",
    "medication_name_too_long",
    "medication_dosage_too_long",
    "medication_instructions_too_long",
    "medication_submission_id_invalid",
}


def create_medications_router(get_runtime: Callable[[], SystemRuntime]) -> APIRouter:
    router = APIRouter()
    phr_sync_lock = asyncio.Lock()

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
        submission_id: str = Form(""),
        session: Session = Depends(get_session),
    ):
        try:
            effective_medication_name = resolve_choice_value(medication_choice, medication_name_custom, medication_name)
            has_dosage_value = (dosage_choice and dosage_choice != CUSTOM_CHOICE) or (dosage_custom and dosage_custom.strip()) or (dosage and dosage.strip())
            effective_dosage = resolve_choice_value(dosage_choice, dosage_custom, dosage) if has_dosage_value else ""
            effective_times_csv = resolve_schedule_times(schedule_template, times_csv)
            parsed_start_date = _parse_medication_date(start_date)
            parsed_end_date = _parse_medication_date(end_date)
            if parsed_end_date < parsed_start_date:
                raise ValueError("invalid_date_range")
            parse_times_csv(effective_times_csv)
            effective_medication_name = effective_medication_name.strip()
            effective_dosage = effective_dosage.strip()
            normalized_instructions = instructions.strip()
            normalized_submission_id = normalize_medication_submission_id(submission_id)
            if len(effective_medication_name) > 255:
                raise ValueError("medication_name_too_long")
            if len(effective_dosage) > 255:
                raise ValueError("medication_dosage_too_long")
            if len(normalized_instructions) > 2000:
                raise ValueError("medication_instructions_too_long")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=public_error_code(exc, allowed_codes=MEDICATION_FORM_ERROR_CODES, fallback="medication_form_invalid"),
            ) from exc

        try:
            with get_runtime().write_lock:
                create_medication_plan(
                    session,
                    medication_name=effective_medication_name,
                    dosage=effective_dosage,
                    start_date=parsed_start_date,
                    end_date=parsed_end_date,
                    times_csv=effective_times_csv,
                    instructions=normalized_instructions,
                    submission_id=normalized_submission_id,
                )
        except MedicationSubmissionConflictError as exc:
            raise HTTPException(status_code=409, detail=exc.code) from exc
        return hx_refresh()

    @router.post("/medications/{plan_id}/delete")
    async def remove_medication(plan_id: int, session: Session = Depends(get_session)):
        with get_runtime().write_lock:
            deleted = delete_medication_plan(session, plan_id)
            if not deleted:
                raise HTTPException(status_code=404, detail="medication_plan_not_found")
        return hx_refresh()

    @router.post("/phr/register")
    async def register_phr_patient(session: Session = Depends(get_session)):
        async with phr_sync_lock:
            runtime = get_runtime()
            with runtime.write_lock:
                medications = build_phr_registration_items(session)
                if not medications:
                    raise HTTPException(status_code=409, detail="phr_medication_required")
                medication_fingerprint = phr_medication_snapshot_fingerprint(medications)
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
                    mark_phr_sync_failed(session, public_phr_sync_failure_message(exc))
                    session.commit()
                return hx_refresh()

            with runtime.write_lock:
                apply_phr_registration_result(
                    session,
                    result,
                    expected_medication_fingerprint=medication_fingerprint,
                )
                session.commit()
        return hx_refresh()

    return router


def _parse_medication_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid_date_format") from exc
