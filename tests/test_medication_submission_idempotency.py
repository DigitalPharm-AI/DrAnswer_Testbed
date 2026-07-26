from __future__ import annotations

import threading
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from system_app.db import get_session
from system_app.migrations import run_migrations
from system_app.models import DoseEvent, DoseSchedule, MedicationPlan
from system_app.routes.medications import create_medications_router
from tests.helpers import build_threadsafe_session_factory


def _isolated_app():
    session_factory = build_threadsafe_session_factory()
    runtime = type(
        "Runtime",
        (),
        {
            "write_lock": threading.RLock(),
            "phr_client": None,
        },
    )()
    app = FastAPI()
    app.include_router(create_medications_router(lambda: runtime))

    def session_dependency():
        with session_factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_dependency
    return app, session_factory


def _medication_form(**overrides) -> dict[str, str]:
    values = {
        "medication_choice": "혈압약",
        "dosage_choice": "1정",
        "schedule_template": "morning_evening",
        "start_date": "2026-04-20",
        "end_date": "2026-04-20",
        "instructions": "식후 복용",
    }
    values.update(overrides)
    return values


def _row_counts(session_factory) -> tuple[int, int, int]:
    with session_factory() as session:
        return (
            session.query(MedicationPlan).count(),
            session.query(DoseSchedule).count(),
            session.query(DoseEvent).count(),
        )


def test_same_medication_submission_id_replays_without_duplicate_rows():
    app, session_factory = _isolated_app()
    client = TestClient(app)
    submission_id = str(uuid4())
    payload = _medication_form(submission_id=submission_id)

    first_response = client.post("/medications", data=payload)
    first_counts = _row_counts(session_factory)
    second_response = client.post("/medications", data=payload)

    with session_factory() as session:
        plan = session.query(MedicationPlan).one()

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert first_counts == (1, 2, 2)
    assert _row_counts(session_factory) == first_counts
    assert plan.submission_id == submission_id


def test_different_medication_submission_ids_create_independent_plans():
    app, session_factory = _isolated_app()
    client = TestClient(app)

    first_response = client.post(
        "/medications",
        data=_medication_form(submission_id=str(uuid4())),
    )
    second_response = client.post(
        "/medications",
        data=_medication_form(submission_id=str(uuid4())),
    )

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert _row_counts(session_factory) == (2, 4, 4)


def test_legacy_medication_submissions_without_id_remain_independent():
    app, session_factory = _isolated_app()
    client = TestClient(app)
    payload = _medication_form()

    first_response = client.post("/medications", data=payload)
    second_response = client.post("/medications", data=payload)

    with session_factory() as session:
        submission_ids = [
            value
            for (value,) in session.query(MedicationPlan.submission_id)
            .order_by(MedicationPlan.id.asc())
            .all()
        ]

    assert first_response.status_code == 204
    assert second_response.status_code == 204
    assert _row_counts(session_factory) == (2, 4, 4)
    assert submission_ids == [None, None]


def test_reusing_medication_submission_id_with_changed_payload_conflicts():
    app, session_factory = _isolated_app()
    client = TestClient(app)
    submission_id = str(uuid4())

    first_response = client.post(
        "/medications",
        data=_medication_form(submission_id=submission_id),
    )
    conflict_response = client.post(
        "/medications",
        data=_medication_form(submission_id=submission_id, dosage_choice="2정"),
    )

    assert first_response.status_code == 204
    assert conflict_response.status_code == 409
    assert conflict_response.json()["detail"] == "medication_submission_conflict"
    assert _row_counts(session_factory) == (1, 2, 2)


def test_medication_submission_id_is_validated_and_canonicalized():
    app, session_factory = _isolated_app()
    client = TestClient(app)
    raw_submission_id = uuid4().hex

    invalid_response = client.post(
        "/medications",
        data=_medication_form(submission_id="not-a-uuid"),
    )
    valid_response = client.post(
        "/medications",
        data=_medication_form(submission_id=raw_submission_id),
    )

    with session_factory() as session:
        plan = session.query(MedicationPlan).one()

    assert invalid_response.status_code == 422
    assert invalid_response.json()["detail"] == "medication_submission_id_invalid"
    assert valid_response.status_code == 204
    assert plan.submission_id == str(UUID(raw_submission_id))


def test_legacy_medication_plan_schema_is_upgraded_with_nullable_unique_key():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE medication_plans (
                    id INTEGER NOT NULL PRIMARY KEY,
                    patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
                    medication_name VARCHAR(255) NOT NULL,
                    dosage VARCHAR(255) NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL DEFAULT '',
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO medication_plans
                    (id, patient_id, medication_name, start_date, end_date)
                VALUES
                    (1, 'demo-patient', 'legacy-a', '2026-04-20', '2026-04-20'),
                    (2, 'demo-patient', 'legacy-b', '2026-04-20', '2026-04-20')
                """
            )
        )

    run_migrations(engine)
    run_migrations(engine)

    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("medication_plans")}
    indexes = {index["name"]: index for index in inspector.get_indexes("medication_plans")}
    assert "submission_id" in columns
    assert bool(indexes["uq_medication_plan_patient_submission"]["unique"]) is True

    with engine.begin() as connection:
        legacy_submission_ids = connection.execute(
            text("SELECT submission_id FROM medication_plans ORDER BY id")
        ).scalars().all()
        connection.execute(
            text(
                """
                INSERT INTO medication_plans
                    (id, patient_id, submission_id, medication_name, start_date, end_date)
                VALUES
                    (3, 'demo-patient', NULL, 'legacy-c', '2026-04-20', '2026-04-20'),
                    (4, 'demo-patient', :submission_id, 'new-a', '2026-04-20', '2026-04-20')
                """
            ),
            {"submission_id": str(uuid4())},
        )

    assert legacy_submission_ids == [None, None]

    duplicate_id = str(uuid4())
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO medication_plans
                        (id, patient_id, submission_id, medication_name, start_date, end_date)
                    VALUES
                        (5, 'demo-patient', :submission_id, 'new-b', '2026-04-20', '2026-04-20'),
                        (6, 'demo-patient', :submission_id, 'new-c', '2026-04-20', '2026-04-20')
                    """
                ),
                {"submission_id": duplicate_id},
            )
