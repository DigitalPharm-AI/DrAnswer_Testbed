from __future__ import annotations

from shared.json_utils import parse_json_list, parse_json_object
from system_app.models import SideEffectRecord


def side_effect_record_view(record: SideEffectRecord) -> dict:
    return {
        "id": record.public_id,
        "patient_id": record.patient_id,
        "medication_name": record.medication_name,
        "symptom_text": record.symptom_text,
        "symptom_onset_text": record.symptom_onset_text,
        "suspected": record.suspected,
        "severity": parse_json_object(record.severity_result_json),
        "matched_effects": parse_json_list(record.matched_effects_json),
        "matched_items": parse_json_list(record.matched_items_json),
        "related_dose_event_id": None,
        "version": record.version,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
