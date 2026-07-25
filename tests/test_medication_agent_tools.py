from __future__ import annotations

from datetime import date, datetime

from fastapi.testclient import TestClient

from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.names import (
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_SIDE_EFFECT_HISTORY,
    MEDICATION_CHAT_TOOLS,
    SIDE_EFFECT_TOOLS,
)
from shared.settings import get_settings
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import DoseEvent, DoseSchedule, MedicationPlan, SideEffectRecord


def _internal_headers() -> dict[str, str]:
    token = get_settings().internal_api_token
    return {"X-Internal-Api-Token": token} if token else {}


def test_medication_read_tools_are_cataloged_for_medication_chat():
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}

    assert GET_MEDICATION_DOSE_STATUS in MEDICATION_CHAT_TOOLS
    assert CREATE_MEDICATION_SIDE_EFFECT_RECORD in MEDICATION_CHAT_TOOLS
    assert CREATE_MEDICATION_SIDE_EFFECT_RECORD not in SIDE_EFFECT_TOOLS
    assert tools[CREATE_MEDICATION_SIDE_EFFECT_RECORD]["_meta"]["mutability"] == "write"
    assert tools[CREATE_MEDICATION_SIDE_EFFECT_RECORD]["_meta"]["confirmation_policy"] == "user_required"
    assert GET_SIDE_EFFECT_HISTORY in MEDICATION_CHAT_TOOLS
    assert tools[GET_MEDICATION_DOSE_STATUS]["_meta"]["mutability"] == "read"
    assert tools[GET_SIDE_EFFECT_HISTORY]["_meta"]["mutability"] == "read"


def test_agent_side_effect_record_and_history_api():
    client = TestClient(app)
    patient_id = "tool-test-patient-side-effect"

    with SessionLocal() as session:
        session.query(SideEffectRecord).filter(SideEffectRecord.patient_id == patient_id).delete()
        session.commit()

    try:
        create_response = client.post(
            "/api/agent/side-effects/records",
            headers=_internal_headers(),
            json={
                "patient_id": patient_id,
                "phr_patient_key": "phr-tool-test",
                "medication_name": "demo-med",
                "symptom_text": "nausea",
                "suspected": True,
                "severity": "moderate",
                "matched_effects": ["nausea"],
                "matched_items": ["demo-med"],
                "evidence": "matched precaution",
                "recommendation": "monitor and contact clinician if severe",
                "source_trace_id": "trace-side-effect-tool-test",
                "source_event_type": "multiturn_chat",
            },
        )

        assert create_response.status_code == 200
        created = create_response.json()["record"]
        assert created["patient_id"] == patient_id
        assert created["suspected"] is True
        assert created["matched_items"] == ["demo-med"]

        duplicate_response = client.post(
            "/api/agent/side-effects/records",
            headers=_internal_headers(),
            json={
                "patient_id": patient_id,
                "medication_name": "demo-med",
                "symptom_text": "nausea",
                "suspected": True,
                "severity": "moderate",
                "source_trace_id": "trace-side-effect-tool-test",
                "source_event_type": "multiturn_chat",
            },
        )
        assert duplicate_response.status_code == 200
        assert duplicate_response.json()["record"]["id"] == created["id"]
        with SessionLocal() as session:
            duplicate_count = session.query(SideEffectRecord).filter(SideEffectRecord.source_trace_id == "trace-side-effect-tool-test").count()
        assert duplicate_count == 1

        second_response = client.post(
            "/api/agent/side-effects/records",
            headers=_internal_headers(),
            json={
                "patient_id": patient_id,
                "phr_patient_key": "phr-tool-test",
                "medication_name": "demo-med",
                "symptom_text": "headache",
                "suspected": False,
                "severity": "none",
                "source_trace_id": "trace-side-effect-tool-test-2",
                "source_event_type": "multiturn_chat",
            },
        )
        assert second_response.status_code == 200
        second = second_response.json()["record"]
        with SessionLocal() as session:
            first_record = session.get(SideEffectRecord, created["id"])
            second_record = session.get(SideEffectRecord, second["id"])
            first_record.created_at = datetime(2026, 4, 20, 10, 0)
            second_record.created_at = datetime(2026, 5, 2, 10, 0)
            session.commit()

        history_response = client.get(
            "/api/agent/side-effects/history",
            headers=_internal_headers(),
            params={"patient_id": patient_id, "start_date": "2026-04-19", "end_date": "2026-04-21", "suspected": True, "limit": 5},
        )

        assert history_response.status_code == 200
        payload = history_response.json()
        assert payload["total"] == 1
        assert payload["start_date"] == "2026-04-19"
        assert payload["end_date"] == "2026-04-21"
        assert payload["records"][0]["id"] == created["id"]
    finally:
        with SessionLocal() as session:
            session.query(SideEffectRecord).filter(SideEffectRecord.patient_id == patient_id).delete()
            session.commit()


def test_agent_medication_dose_status_api_returns_patient_day_events():
    client = TestClient(app)
    patient_id = "tool-test-patient-dose-status"
    target_date = date(2026, 4, 20)

    with SessionLocal() as session:
        _delete_patient_medication_rows(session, patient_id)
        plan = MedicationPlan(
            patient_id=patient_id,
            medication_name="demo-med",
            dosage="1 tab",
            instructions="after meal",
            start_date=target_date,
            end_date=target_date,
            active=True,
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(plan_id=plan.id, slot_label="morning", scheduled_time="09:00")
        session.add(schedule)
        session.flush()
        session.add(
            DoseEvent(
                patient_id=patient_id,
                plan_id=plan.id,
                schedule_id=schedule.id,
                medication_name=plan.medication_name,
                slot_label=schedule.slot_label,
                scheduled_for=datetime(2026, 4, 20, 9, 0),
                taken_at=datetime(2026, 4, 20, 9, 5),
                status="taken",
            )
        )
        session.add(
            DoseEvent(
                patient_id=patient_id,
                plan_id=plan.id,
                schedule_id=schedule.id,
                medication_name=plan.medication_name,
                slot_label=schedule.slot_label,
                scheduled_for=datetime(2026, 4, 21, 9, 0),
                status="missed",
            )
        )
        session.commit()

    try:
        response = client.get(
            "/api/agent/dose-events",
            headers=_internal_headers(),
            params={"patient_id": patient_id, "target_date": target_date.isoformat()},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["patient_id"] == patient_id
        assert payload["target_date"] == "2026-04-20"
        assert payload["start_date"] == "2026-04-20"
        assert payload["end_date"] == "2026-04-20"
        assert payload["total"] == 1
        assert payload["dose_events"][0]["status"] == "taken"
        assert payload["dose_events"][0]["medication_name"] == "demo-med"

        range_response = client.get(
            "/api/agent/dose-events",
            headers=_internal_headers(),
            params={"patient_id": patient_id, "start_date": "2026-04-20", "end_date": "2026-04-21"},
        )
        assert range_response.status_code == 200
        range_payload = range_response.json()
        assert range_payload["target_date"] is None
        assert range_payload["start_date"] == "2026-04-20"
        assert range_payload["end_date"] == "2026-04-21"
        assert range_payload["total"] == 2
        assert range_payload["totals_by_status"]["taken"] == 1
        assert range_payload["totals_by_status"]["missed"] == 1
        assert [row["total"] for row in range_payload["summary_by_date"]] == [1, 1]
    finally:
        with SessionLocal() as session:
            _delete_patient_medication_rows(session, patient_id)
            session.commit()


def _delete_patient_medication_rows(session, patient_id: str) -> None:
    plan_ids = [row[0] for row in session.query(MedicationPlan.id).filter(MedicationPlan.patient_id == patient_id).all()]
    session.query(DoseEvent).filter(DoseEvent.patient_id == patient_id).delete(synchronize_session=False)
    if plan_ids:
        session.query(DoseSchedule).filter(DoseSchedule.plan_id.in_(plan_ids)).delete(synchronize_session=False)
    session.query(MedicationPlan).filter(MedicationPlan.patient_id == patient_id).delete(synchronize_session=False)
