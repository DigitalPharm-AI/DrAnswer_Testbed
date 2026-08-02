from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest

from agent_app.jobs import worker
from agent_app.llm.messages import build_chat_messages
from agent_app.tools.backend_query import BackendReadContractError
from shared.async_v13_contracts import MissedDoseEventRequest
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION

REQUEST_ID = "req_0000000012345678"
PATIENT_ID = "patient_0000000012345678"
DOSE_EVENT_ID = "dose_0000000012345678"


class _RecordingBackendQueries:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def missed_dose_event_context(
        self,
        *,
        patient_id: str,
        dose_event_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "patient_id": patient_id,
                "dose_event_id": dose_event_id,
                "request_id": request_id,
            }
        )
        patient_snapshot = {
            "patient_id": patient_id,
            "as_of": datetime(2026, 7, 25, 9, 30).isoformat(),
            "date": "2026-07-25",
            "read_contract_version": BACKEND_READ_CONTRACT_VERSION,
            "context_mode": "complete",
            "availability": {
                "profile": "available",
                "conditions_and_treatments": "available",
                "today_medication": "available",
                "today_meals": "not_found",
                "notification_policies": "not_found",
            },
            "profile": {"patient_id": patient_id, "age": 55},
            "active_conditions": ["hypertension"],
            "active_treatments": ["고혈압약"],
            "active_medication_schedules": [
                {
                    "patient_id": patient_id,
                    "medication_name": "암로디핀 5mg",
                    "treatment_area": "고혈압약",
                    "version": 3,
                }
            ],
            "today_medication": {
                "schedules": [],
                "dose_events": [
                    {
                        "patient_id": patient_id,
                        "dose_event_id": DOSE_EVENT_ID,
                        "medication_name": "암로디핀 5mg",
                        "version": 4,
                    }
                ],
                "total": 1,
                "totals_by_status": {"missed": 1},
            },
            "today_meals": [],
            "active_notification_policies": [],
        }
        return {
            "patient_id": patient_id,
            "dose_event_id": dose_event_id,
            "medication_name": "암로디핀 5mg",
            "slot_label": "아침",
            "scheduled_for": datetime(2026, 7, 25, 8, 0),
            "status": "missed",
            "version": 4,
            "patient_snapshot": patient_snapshot,
            "adherence_pattern_context": {
                "pattern_code": "B",
                "reason": "복약 루틴 형성을 위한 단기 미복용 확인이 필요합니다.",
            },
            "tone_policy_context": {
                "pattern_code": "B",
                "tone_key": "persuasion",
                "message": "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
                "message_variant": "v1",
                "message_catalog_source": "test_catalog",
            },
        }


def _missed_dose_payload() -> dict[str, Any]:
    payload = MissedDoseEventRequest(
        request_id=REQUEST_ID,
        patient_id=PATIENT_ID,
        dose_event_id=DOSE_EVENT_ID,
    ).model_dump(mode="json")
    payload["_contract_request_hash"] = "internal-queue-metadata"
    return payload


def test_missed_dose_worker_injects_current_trusted_and_llm_safe_snapshots() -> None:
    backend_queries = _RecordingBackendQueries()
    invoked_payload = worker._missed_dose_payload_with_patient_context(
        _missed_dose_payload(),
        backend_queries=backend_queries,
    )

    assert backend_queries.calls == [
        {
            "patient_id": PATIENT_ID,
            "dose_event_id": DOSE_EVENT_ID,
            "request_id": REQUEST_ID,
        }
    ]
    assert invoked_payload["adherence_pattern_context"]["pattern_code"] == "B"
    assert invoked_payload["tone_policy_context"]["tone_key"] == "persuasion"

    context = invoked_payload["context"]
    trusted = context["trusted_patient_context"]
    assert trusted["patient_id"] == PATIENT_ID
    assert (
        trusted["today_medication"]["dose_events"][0]["dose_event_id"]
        == DOSE_EVENT_ID
    )

    llm_snapshot = context["patient_context_snapshot"]
    llm_snapshot_json = json.dumps(llm_snapshot, ensure_ascii=False)
    assert PATIENT_ID not in llm_snapshot_json
    assert DOSE_EVENT_ID not in llm_snapshot_json
    assert "dose_event_id" not in llm_snapshot_json
    assert '"version"' not in llm_snapshot_json

    llm_message = build_chat_messages("system", invoked_payload)[1]
    serialized_llm_payload = str(llm_message.content)
    assert "trusted_patient_context" not in serialized_llm_payload
    assert PATIENT_ID not in serialized_llm_payload
    assert DOSE_EVENT_ID not in serialized_llm_payload


def test_missed_dose_worker_fails_closed_without_backend_read_database() -> None:
    with pytest.raises(
        BackendReadContractError,
        match="backend_read_database_unavailable",
    ):
        worker._missed_dose_payload_with_patient_context(
            _missed_dose_payload(),
            backend_queries=None,
        )
