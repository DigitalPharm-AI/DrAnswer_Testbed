from fastapi.testclient import TestClient

from phr_app.db import SessionLocal
from phr_app.main import app
from phr_app.models import PhrSideEffectAssessment
from shared.settings import get_settings


def register_patient(client: TestClient, medications: list[dict]) -> str:
    response = client.post("/phr/patients/register", json={"medications": medications})
    assert response.status_code == 200
    return response.json()["phr_patient_key"]


def test_phr_registers_patient_key_and_returns_item_precautions():
    with TestClient(app) as client:
        phr_patient_key = register_patient(
            client,
            [
                {"item_name": "혈압약", "dosage": "1정"},
                {"item_name": "당뇨약", "dosage": "1정"},
            ],
        )
        response = client.get(f"/phr/patients/{phr_patient_key}/medications")

    assert response.status_code == 200
    payload = response.json()
    assert payload["phr_patient_key"] == phr_patient_key
    medications = payload["medications"]
    assert {row["item_name"] for row in medications} == {"혈압약", "당뇨약"}
    assert all("precautions_text" in row for row in medications)


def test_phr_side_effect_assessment_matches_registered_patient_precautions():
    with TestClient(app) as client:
        phr_patient_key = register_patient(client, [{"item_name": "혈압약", "dosage": "1정"}])
        response = client.post(
            "/phr/side-effects/assess",
            json={
                "phr_patient_key": phr_patient_key,
                "medication_name": "혈압약",
                "symptom_text": "약 먹고 어지럽고 두근거려요",
                "recent_chat": [],
                "dose_event_id": 10,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["suspected"] is True
    assert payload["severity"] in {"moderate", "high"}
    assert payload["matched_items"] == ["혈압약"]
    assert any("혈압약" in effect for effect in payload["matched_effects"])


def test_phr_side_effect_assessment_returns_matched_symptom_name_for_nausea():
    with TestClient(app) as client:
        phr_patient_key = register_patient(client, [{"item_name": "당뇨약", "dosage": "1정"}])
        response = client.post(
            "/phr/side-effects/assess",
            json={
                "phr_patient_key": phr_patient_key,
                "symptom_text": "속이 메스꺼운데 약때문일까?",
                "recent_chat": [],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["suspected"] is True
    assert "당뇨약: 메스꺼움" in payload["matched_effects"]
    assert all("주의사항 관련 증상" not in effect for effect in payload["matched_effects"])


def test_phr_side_effect_assessment_ignores_unregistered_item_precautions():
    with TestClient(app) as client:
        phr_patient_key = register_patient(client, [{"item_name": "당뇨약", "dosage": "1정"}])
        response = client.post(
            "/phr/side-effects/assess",
            json={
                "phr_patient_key": phr_patient_key,
                "symptom_text": "근육통이 심하고 몸이 쑤셔요",
                "recent_chat": [],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["suspected"] is False
    assert payload["severity"] == "none"


def test_phr_update_patient_medications_replaces_active_items():
    with TestClient(app) as client:
        phr_patient_key = register_patient(client, [{"item_name": "혈압약", "dosage": "1정"}])
        update_response = client.put(
            f"/phr/patients/{phr_patient_key}/medications",
            json={"medications": [{"item_name": "고지혈증약", "dosage": "1정"}]},
        )
        list_response = client.get(f"/phr/patients/{phr_patient_key}/medications")

    assert update_response.status_code == 200
    assert {row["item_name"] for row in list_response.json()["medications"]} == {"고지혈증약"}


def test_phr_read_only_mode_rejects_writes_but_keeps_reads(monkeypatch):
    monkeypatch.setenv("PHR_READ_ONLY", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            register_response = client.post("/phr/patients/register", json={"medications": [{"item_name": "혈압약", "dosage": "1정"}]})
            list_response = client.get("/phr/patients/seed-phr-patient-key/medications")
    finally:
        get_settings.cache_clear()

    assert register_response.status_code == 503
    assert register_response.json()["detail"] == "phr_read_only_mode"
    assert list_response.status_code == 200


def test_phr_side_effect_assessment_does_not_persist_domain_record():
    with SessionLocal() as session:
        session.query(PhrSideEffectAssessment).delete()
        session.commit()

    with TestClient(app) as client:
        phr_patient_key = register_patient(client, [{"item_name": "당뇨약", "dosage": "1정"}])
        response = client.post(
            "/phr/side-effects/assess",
            json={
                "phr_patient_key": phr_patient_key,
                "symptom_text": "속이 메스꺼워요",
                "recent_chat": [],
            },
        )

    assert response.status_code == 200
    with SessionLocal() as session:
        assert session.query(PhrSideEffectAssessment).count() == 0
