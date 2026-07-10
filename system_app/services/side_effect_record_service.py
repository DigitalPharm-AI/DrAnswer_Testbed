from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_list, parse_json_object
from shared.schemas import SideEffectRecordRequest
from shared.settings import get_settings
from system_app.models import DoseEvent, SideEffectRecord


def record_side_effect(session: Session, request: SideEffectRecordRequest) -> SideEffectRecord:
    related_event = session.get(DoseEvent, request.related_dose_event_id) if request.related_dose_event_id else None
    record = SideEffectRecord(
        patient_id=request.patient_id or (related_event.patient_id if related_event else get_settings().patient_id),
        phr_patient_key=request.phr_patient_key or "",
        medication_name=request.medication_name or (related_event.medication_name if related_event else ""),
        symptom_text=request.symptom_text,
        suspected=request.suspected,
        severity=request.severity,
        matched_effects_json=dump_json(request.matched_effects),
        matched_items_json=dump_json(request.matched_items),
        evidence=request.evidence,
        recommendation=request.recommendation,
        source_trace_id=request.source_trace_id or "",
        source_event_type=request.source_event_type,
        related_dose_event_id=request.related_dose_event_id,
        metadata_json=dump_json(request.metadata),
    )
    session.add(record)
    session.flush()
    return record


def list_side_effect_history(
    session: Session,
    *,
    patient_id: str | None = None,
    limit: int = 20,
    suspected: bool | None = None,
    medication_name: str | None = None,
    severity: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[SideEffectRecord]:
    stmt = select(SideEffectRecord)
    target_patient_id = patient_id or get_settings().patient_id
    if target_patient_id:
        stmt = stmt.where(SideEffectRecord.patient_id == target_patient_id)
    if suspected is not None:
        stmt = stmt.where(SideEffectRecord.suspected.is_(suspected))
    if medication_name:
        stmt = stmt.where(SideEffectRecord.medication_name == medication_name)
    if severity:
        stmt = stmt.where(SideEffectRecord.severity == severity)
    if start_date:
        stmt = stmt.where(SideEffectRecord.created_at >= datetime.combine(start_date, time.min))
    if end_date:
        stmt = stmt.where(SideEffectRecord.created_at <= datetime.combine(end_date, time.max))
    stmt = stmt.order_by(desc(SideEffectRecord.created_at), desc(SideEffectRecord.id)).limit(max(1, min(limit, 100)))
    return list(session.scalars(stmt).all())


def side_effect_record_view(record: SideEffectRecord) -> dict:
    return {
        "id": record.id,
        "patient_id": record.patient_id,
        "phr_patient_key": record.phr_patient_key,
        "medication_name": record.medication_name,
        "symptom_text": record.symptom_text,
        "suspected": record.suspected,
        "severity": record.severity,
        "matched_effects": parse_json_list(record.matched_effects_json),
        "matched_items": parse_json_list(record.matched_items_json),
        "evidence": record.evidence,
        "recommendation": record.recommendation,
        "source_trace_id": record.source_trace_id,
        "source_event_type": record.source_event_type,
        "related_dose_event_id": record.related_dose_event_id,
        "metadata": parse_json_object(record.metadata_json),
        "created_at": record.created_at,
    }
