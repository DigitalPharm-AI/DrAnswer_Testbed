from __future__ import annotations

import asyncio
import threading
from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from shared.schemas import (
    PhrMedicationRegistrationItem,
    PhrPatientRegistrationResult,
    PhrRegisteredMedication,
)
from shared.settings import get_settings
from system_app.db import get_session
from system_app.models import MedicationPlan, SimulationPatientProfile
from system_app.routes.medications import create_medications_router
from system_app.services.patient_profile_service import (
    phr_medication_snapshot_fingerprint,
)
from tests.helpers import build_threadsafe_session_factory


settings = get_settings()


def _isolated_app(*, phr_client):
    session_factory = build_threadsafe_session_factory()
    runtime = SimpleNamespace(
        write_lock=threading.RLock(),
        phr_client=phr_client,
    )
    app = FastAPI()
    app.include_router(create_medications_router(lambda: runtime))

    def session_dependency():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    return app, session_factory


def _medication(
    *,
    patient_id: str,
    medication_name: str,
    dosage: str = "1 tablet",
) -> MedicationPlan:
    return MedicationPlan(
        patient_id=patient_id,
        medication_name=medication_name,
        dosage=dosage,
        instructions="",
        start_date=date(2026, 4, 20),
        end_date=date(2026, 4, 30),
        active=True,
    )


class CapturingPhrClient:
    def __init__(self, *, phr_patient_key: str = "phr_captured") -> None:
        self.phr_patient_key = phr_patient_key
        self.medications: list[PhrMedicationRegistrationItem] = []

    async def register_patient(self, medications):
        self.medications = list(medications)
        return _registration_result(self.phr_patient_key, medications)

    async def update_patient_medications(self, phr_patient_key: str, medications):
        self.medications = list(medications)
        return _registration_result(phr_patient_key, medications)


class DelayedPhrClient(CapturingPhrClient):
    def __init__(self, *, phr_patient_key: str) -> None:
        super().__init__(phr_patient_key=phr_patient_key)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def register_patient(self, medications):
        self.medications = list(medications)
        self.started.set()
        await self.release.wait()
        return _registration_result(self.phr_patient_key, medications)

    async def update_patient_medications(self, phr_patient_key: str, medications):
        self.medications = list(medications)
        self.started.set()
        await self.release.wait()
        return _registration_result(phr_patient_key, medications)


def _registration_result(
    phr_patient_key: str,
    medications,
) -> PhrPatientRegistrationResult:
    return PhrPatientRegistrationResult(
        phr_patient_key=phr_patient_key,
        medications=[
            PhrRegisteredMedication(
                item_name=item.item_name,
                dosage=item.dosage,
                active=True,
            )
            for item in medications
        ],
    )


def test_phr_snapshot_fingerprint_is_deterministic_and_counts_duplicates():
    first = PhrMedicationRegistrationItem(item_name="Drug B", dosage="2 tablets")
    second = PhrMedicationRegistrationItem(item_name="Drug A", dosage="1 tablet")

    ordered = phr_medication_snapshot_fingerprint([first, second])
    reversed_order = phr_medication_snapshot_fingerprint([second, first])
    with_duplicate = phr_medication_snapshot_fingerprint([second, first, second])

    assert ordered == reversed_order
    assert with_duplicate != ordered


@pytest.mark.asyncio
async def test_phr_registration_excludes_foreign_patient_medications():
    phr_client = CapturingPhrClient()
    app, session_factory = _isolated_app(phr_client=phr_client)
    with session_factory() as session:
        session.add_all(
            [
                _medication(
                    patient_id=settings.patient_id,
                    medication_name="Current patient drug",
                ),
                _medication(
                    patient_id="foreign-patient",
                    medication_name="Foreign patient drug",
                ),
            ]
        )
        session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/phr/register")

    assert response.status_code == 204
    assert [item.item_name for item in phr_client.medications] == [
        "Current patient drug"
    ]


@pytest.mark.asyncio
async def test_medication_added_during_phr_registration_keeps_new_key_but_needs_sync():
    phr_client = DelayedPhrClient(phr_patient_key="phr_returned_after_add")
    app, session_factory = _isolated_app(phr_client=phr_client)
    with session_factory() as session:
        session.add(
            _medication(
                patient_id=settings.patient_id,
                medication_name="Initial drug",
            )
        )
        session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registration = asyncio.create_task(client.post("/phr/register"))
        await phr_client.started.wait()
        with session_factory() as session:
            session.add(
                _medication(
                    patient_id=settings.patient_id,
                    medication_name="Added during PHR await",
                )
            )
            session.commit()
        phr_client.release.set()
        response = await registration

    with session_factory() as session:
        profile = session.query(SimulationPatientProfile).one()
        phr_patient_key = profile.phr_patient_key
        sync_status = profile.sync_status

    assert response.status_code == 204
    assert phr_patient_key == "phr_returned_after_add"
    assert sync_status == "needs_sync"


@pytest.mark.asyncio
async def test_medication_deleted_during_phr_update_keeps_existing_key_but_needs_sync():
    phr_client = DelayedPhrClient(phr_patient_key="phr_returned_after_delete")
    app, session_factory = _isolated_app(phr_client=phr_client)
    with session_factory() as session:
        removed = _medication(
            patient_id=settings.patient_id,
            medication_name="Removed during PHR await",
        )
        session.add_all(
            [
                _medication(
                    patient_id=settings.patient_id,
                    medication_name="Remaining drug",
                ),
                removed,
            ]
        )
        session.add(
            SimulationPatientProfile(
                local_patient_id=settings.patient_id,
                phr_patient_key="phr_existing_before_delete",
                sync_status="needs_sync",
                error_message="",
            )
        )
        session.commit()
        removed_id = removed.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        registration = asyncio.create_task(client.post("/phr/register"))
        await phr_client.started.wait()
        with session_factory() as session:
            session.delete(session.get(MedicationPlan, removed_id))
            session.commit()
        phr_client.release.set()
        response = await registration

    with session_factory() as session:
        profile = session.query(SimulationPatientProfile).one()
        phr_patient_key = profile.phr_patient_key
        sync_status = profile.sync_status

    assert response.status_code == 204
    assert phr_patient_key == "phr_existing_before_delete"
    assert sync_status == "needs_sync"
