from fastapi.testclient import TestClient

import system_app.main as system_main
from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import MedicationPlan, SimulationPatientProfile
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.patient_profile_service import ensure_base_data
from system_app.services.phr_client import PhrServiceError


class FailingPhrClient:
    async def register_patient(self, medications):
        raise PhrServiceError(
            "pytest private upstream PHR message peanut allergy token=secret-value",
            status_code=500,
            detail="upstream_private_detail",
        )

    async def update_patient_medications(self, phr_patient_key: str, medications):
        raise PhrServiceError(
            "pytest private upstream PHR update message peanut allergy token=secret-value",
            status_code=500,
            detail="upstream_private_detail",
        )


def test_medication_form_accepts_preset_schedule_and_choices():
    client = TestClient(app)

    response = client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "테스트 등록",
        },
    )

    assert response.status_code == 204

    page = client.get("/")
    assert page.status_code == 200
    assert "혈압약" in page.text
    assert "아침 08:00" in page.text
    assert "야간 21:00" in page.text


def test_medication_form_accepts_custom_name_and_dosage():
    client = TestClient(app)

    response = client.post(
        "/medications",
        data={
            "medication_choice": "__custom__",
            "medication_name_custom": "맞춤약",
            "dosage_choice": "__custom__",
            "dosage_custom": "반정",
            "schedule_template": "morning_lunch_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "커스텀 등록",
        },
    )

    assert response.status_code == 204

    page = client.get("/")
    assert page.status_code == 200
    assert "맞춤약" in page.text
    assert "반정" in page.text


def test_medication_form_validation_returns_public_error_code():
    client = TestClient(app)

    response = client.post(
        "/medications",
        data={
            "medication_choice": "__custom__",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "required_choice_missing"


def test_medication_form_rejects_invalid_dates_with_public_error_codes():
    client = TestClient(app)
    raw_date = "2026-04-20-private-peanut-allergy"

    response = client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": raw_date,
            "end_date": "2026-04-20",
        },
    )
    range_response = client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-21",
            "end_date": "2026-04-20",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_date_format"
    assert raw_date not in response.text
    assert range_response.status_code == 422
    assert range_response.json()["detail"] == "invalid_date_range"


def test_medication_form_rejects_invalid_custom_time_with_public_error_code():
    client = TestClient(app)
    raw_time = "25:99-private-peanut-allergy"

    response = client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "__custom__",
            "times_csv": raw_time,
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_schedule_time_format"
    assert raw_time not in response.text


def test_phr_register_failure_stores_public_failure_copy(monkeypatch):
    client = TestClient(app)
    monkeypatch.setattr(system_main, "phr_client", FailingPhrClient())

    with SessionLocal() as session:
        reset_simulation_state(session)
        session.query(SimulationPatientProfile).delete()
        ensure_base_data(session)
        session.commit()

    client.post(
        "/medications",
        data={
            "medication_choice": "혈압약",
            "dosage_choice": "1정",
            "schedule_template": "morning_evening",
            "start_date": "2026-04-20",
            "end_date": "2026-04-20",
            "instructions": "PHR failure redaction",
        },
    )
    response = client.post("/phr/register")

    with SessionLocal() as session:
        profile = session.query(SimulationPatientProfile).one()
        error_message = profile.error_message
        sync_status = profile.sync_status
        reset_simulation_state(session)
        session.query(SimulationPatientProfile).delete()
        session.commit()

    assert response.status_code == 204
    assert sync_status == "sync_failed"
    assert "PHR 서버와 연결하지 못했습니다" in error_message
    assert "pytest private upstream PHR message" not in error_message
    assert "peanut allergy" not in error_message
    assert "secret-value" not in error_message
