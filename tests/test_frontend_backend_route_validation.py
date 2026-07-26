from __future__ import annotations

import asyncio
import threading
from datetime import date, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared.schemas import PhrPatientRegistrationResult, PhrRegisteredMedication
from system_app.db import get_session
from system_app.main import app as system_app
from system_app.models import DoseEvent, MedicationPlan
from system_app.routes.medications import create_medications_router
from system_app.routes.simulation import create_simulation_router
from system_app.services.clock_service import ensure_clock
from system_app.services.medication_plan_service import create_medication_plan
from tests.helpers import build_threadsafe_session_factory


def _isolated_app(*, phr_client=None):
    session_factory = build_threadsafe_session_factory()
    runtime = SimpleNamespace(
        write_lock=threading.RLock(),
        phr_client=phr_client,
    )
    app = FastAPI()
    app.include_router(create_medications_router(lambda: runtime))
    app.include_router(create_simulation_router(lambda: runtime))

    def session_dependency():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    return app, session_factory


def _medication_form(**overrides) -> dict[str, str]:
    values = {
        "medication_choice": "__custom__",
        "medication_name_custom": "테스트약",
        "dosage_choice": "__custom__",
        "dosage_custom": "1정",
        "schedule_template": "morning_evening",
        "start_date": "2026-04-20",
        "end_date": "2026-04-20",
        "instructions": "식후 복용",
    }
    values.update(overrides)
    return values


@pytest.mark.parametrize(
    ("path", "payload", "detail"),
    [
        ("/clock/advance", {"minutes": "29"}, "clock_advance_minutes_invalid"),
        ("/clock/advance", {"minutes": "181"}, "clock_advance_minutes_invalid"),
        ("/clock/play", {"speed_multiplier": "0"}, "clock_speed_multiplier_invalid"),
        ("/clock/play", {"speed_multiplier": "2"}, "clock_speed_multiplier_invalid"),
    ],
)
def test_simulation_controls_reject_values_not_offered_by_the_frontend(path, payload, detail):
    app, _ = _isolated_app()

    response = TestClient(app).post(path, data=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_take_dose_returns_404_when_event_does_not_exist():
    app, _ = _isolated_app()

    response = TestClient(app).post("/doses/999999/take")

    assert response.status_code == 404
    assert response.json()["detail"] == "dose_event_not_found"


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"message": "   "}, "chat_message_required"),
        ({"message": "안녕", "event_type": "unknown"}, "chat_event_type_invalid"),
        ({"message": "안녕", "contract_version": "v9"}, "chat_contract_version_invalid"),
    ],
)
def test_system_chat_rejects_values_not_offered_by_the_frontend(payload, detail):
    response = TestClient(system_app).post("/chat/system", data=payload)

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_adverse_event_response_rejects_blank_text_before_lookup():
    response = TestClient(system_app).post(
        "/chat/ae-response",
        data={"chat_message_id": "999999", "response_text": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "ae_response_required"


@pytest.mark.parametrize(
    ("portion_g", "meal_type", "detail"),
    [
        ("0", "lunch", "invalid_food_portion"),
        ("2001", "lunch", "invalid_food_portion"),
        ("NaN", "lunch", "invalid_food_portion"),
        ("100", "midnight_snack", "invalid_meal_type"),
    ],
)
def test_food_grams_rejects_values_not_offered_by_the_frontend(portion_g, meal_type, detail):
    response = TestClient(system_app).post(
        "/chat/food-grams",
        data={
            "chat_message_id": "999999",
            "portion_g": portion_g,
            "meal_type": meal_type,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_repeated_take_dose_preserves_the_original_taken_timestamp():
    app, session_factory = _isolated_app()
    with session_factory() as session:
        create_medication_plan(
            session,
            medication_name="혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
        )
        event_id = session.query(DoseEvent.id).scalar()
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 8, 5)
        session.commit()

    client = TestClient(app)
    first_response = client.post(f"/doses/{event_id}/take")
    with session_factory() as session:
        original_taken_at = session.get(DoseEvent, event_id).taken_at
        clock = ensure_clock(session)
        clock.current_time = datetime(2026, 4, 20, 10, 0)
        session.commit()

    second_response = client.post(f"/doses/{event_id}/take")
    with session_factory() as session:
        event = session.get(DoseEvent, event_id)

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert original_taken_at == datetime(2026, 4, 20, 8, 5)
    assert event.taken_at == original_taken_at


def test_delete_missing_medication_plan_returns_404():
    app, _ = _isolated_app()

    response = TestClient(app).post("/medications/999999/delete")

    assert response.status_code == 404
    assert response.json()["detail"] == "medication_plan_not_found"


def test_medication_form_trims_persisted_text():
    app, session_factory = _isolated_app()

    response = TestClient(app).post(
        "/medications",
        data=_medication_form(
            medication_name_custom="  맞춤약  ",
            dosage_custom="  반정  ",
            instructions="  식후 복용  ",
        ),
    )
    with session_factory() as session:
        plan = session.query(MedicationPlan).one()

    assert response.status_code == 204
    assert plan.medication_name == "맞춤약"
    assert plan.dosage == "반정"
    assert plan.instructions == "식후 복용"


@pytest.mark.parametrize(
    ("overrides", "detail"),
    [
        ({"medication_name_custom": "약" * 256}, "medication_name_too_long"),
        ({"dosage_custom": "용" * 256}, "medication_dosage_too_long"),
        ({"instructions": "지" * 2001}, "medication_instructions_too_long"),
    ],
)
def test_medication_form_enforces_backend_text_limits(overrides, detail):
    app, _ = _isolated_app()

    response = TestClient(app).post("/medications", data=_medication_form(**overrides))

    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_phr_registration_requires_an_active_medication_plan():
    app, _ = _isolated_app()

    response = TestClient(app).post("/phr/register")

    assert response.status_code == 409
    assert response.json()["detail"] == "phr_medication_required"


class SlowPhrClient:
    def __init__(self) -> None:
        self.register_calls = 0
        self.update_calls = 0

    async def register_patient(self, medications):
        self.register_calls += 1
        await asyncio.sleep(0.05)
        return PhrPatientRegistrationResult(
            phr_patient_key="phr_serialized_registration",
            medications=[
                PhrRegisteredMedication(item_name=item.item_name, dosage=item.dosage, active=True)
                for item in medications
            ],
        )

    async def update_patient_medications(self, phr_patient_key: str, medications):
        self.update_calls += 1
        return PhrPatientRegistrationResult(
            phr_patient_key=phr_patient_key,
            medications=[
                PhrRegisteredMedication(item_name=item.item_name, dosage=item.dosage, active=True)
                for item in medications
            ],
        )


@pytest.mark.asyncio
async def test_concurrent_phr_requests_do_not_duplicate_external_registration():
    phr_client = SlowPhrClient()
    app, session_factory = _isolated_app(phr_client=phr_client)
    with session_factory() as session:
        create_medication_plan(
            session,
            medication_name="혈압약",
            dosage="1정",
            start_date=date(2026, 4, 20),
            end_date=date(2026, 4, 20),
            times_csv="08:00",
        )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(
            client.post("/phr/register"),
            client.post("/phr/register"),
        )

    assert [response.status_code for response in responses] == [204, 204]
    assert phr_client.register_calls == 1
    assert phr_client.update_calls == 1
