from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy.orm import Session

from phr_app import trace_logging
from phr_app.db import SessionLocal, engine, get_session
from phr_app.migrations import run_migrations
from phr_app.models import Base
from phr_app.services import assess_side_effect, list_patient_medications, register_patient_medications, seed_phr_data, update_patient_medications
from shared.schemas import PhrPatientRegistrationRequest, PhrPatientRegistrationResult, SideEffectAssessmentRequest, SideEffectAssessmentResult


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    with SessionLocal() as session:
        seed_phr_data(session)
    yield


app = FastAPI(title="Virtual PHR Service", lifespan=lifespan)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/phr/patients/register", response_model=PhrPatientRegistrationResult)
async def register_patient(payload: PhrPatientRegistrationRequest, session: Session = Depends(get_session)) -> PhrPatientRegistrationResult:
    result = register_patient_medications(session, payload)
    trace_logging.log_info(
        "phr_patient_registered",
        medication_count=len(payload.medications),
        phr_patient_key_present=bool(result.phr_patient_key),
    )
    return result


@app.put("/phr/patients/{phr_patient_key}/medications", response_model=PhrPatientRegistrationResult)
async def update_patient(phr_patient_key: str, payload: PhrPatientRegistrationRequest, session: Session = Depends(get_session)) -> PhrPatientRegistrationResult:
    try:
        result = update_patient_medications(session, phr_patient_key, payload)
    except ValueError as exc:
        if str(exc) == "phr_patient_not_found":
            trace_logging.log_warning("phr_patient_update_failed", reason="phr_patient_not_found")
            raise HTTPException(status_code=404, detail="phr_patient_not_found") from exc
        raise
    trace_logging.log_info(
        "phr_patient_medications_updated",
        medication_count=len(payload.medications),
        phr_patient_key_present=bool(phr_patient_key),
    )
    return result


@app.get("/phr/patients/{phr_patient_key}/medications")
async def patient_medications(phr_patient_key: str, session: Session = Depends(get_session)) -> dict:
    return {"phr_patient_key": phr_patient_key, "medications": list_patient_medications(session, phr_patient_key)}


@app.post("/phr/side-effects/assess", response_model=SideEffectAssessmentResult)
async def side_effect_assessment(payload: SideEffectAssessmentRequest, session: Session = Depends(get_session)) -> SideEffectAssessmentResult:
    trace_logging.log_info(
        "phr_side_effect_assessment_started",
        phr_patient_key_present=bool(payload.phr_patient_key),
        medication_name=trace_logging.snippet(payload.medication_name),
        symptom_text=trace_logging.snippet(payload.symptom_text),
        recent_chat_count=len(payload.recent_chat),
        dose_event_id=payload.dose_event_id,
    )
    try:
        result = assess_side_effect(session, payload)
    except ValueError as exc:
        if str(exc) == "phr_patient_not_found":
            trace_logging.log_warning(
                "phr_side_effect_assessment_failed",
                reason="phr_patient_not_found",
                medication_name=trace_logging.snippet(payload.medication_name),
                symptom_text=trace_logging.snippet(payload.symptom_text),
            )
            raise HTTPException(status_code=404, detail="phr_patient_not_found") from exc
        raise
    trace_logging.log_info(
        "phr_side_effect_assessment_completed",
        suspected=result.suspected,
        severity=result.severity,
        matched_effect_count=len(result.matched_effects),
        matched_items=result.matched_items,
    )
    return result
