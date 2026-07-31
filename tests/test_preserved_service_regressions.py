from datetime import datetime

from sqlalchemy import inspect, text

from system_app.db import SessionLocal
from system_app.migrations import run_migrations
from system_app.models import Base, ChatMessage
from system_app.services.clock_service import ensure_clock
from system_app.services.patient_profile_service import (
    ensure_base_data,
)
from system_app.services.timeline_service import add_chat_message
from tests.helpers import build_system_engine


def test_add_chat_message_uses_simulation_clock_timestamp():
    created_message_id = None
    test_time = datetime(2026, 5, 12, 14, 35, 0)

    with SessionLocal() as session:
        ensure_base_data(session)
        clock = ensure_clock(session)
        clock.current_time = test_time
        message = add_chat_message(
            session,
            role="assistant",
            sender_type="assistant",
            category="missed_dose",
            content="복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
        )
        created_message_id = message.id
        assert message.created_at == test_time
        session.commit()

    with SessionLocal() as session:
        if created_message_id is not None:
            session.query(ChatMessage).filter(
                ChatMessage.id == created_message_id
            ).delete()
            session.commit()


def test_legacy_medication_submission_id_is_dropped_without_losing_plans():
    engine, cleanup = build_system_engine(
        "legacy_medication_plan",
        create_models=False,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE medication_plans (
                    id INTEGER NOT NULL PRIMARY KEY,
                    patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
                    submission_id VARCHAR(36),
                    medication_name VARCHAR(255) NOT NULL,
                    dosage VARCHAR(255) NOT NULL DEFAULT '',
                    instructions TEXT NOT NULL DEFAULT '',
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMP,
                    CONSTRAINT uq_medication_plan_patient_submission
                    UNIQUE (patient_id, submission_id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO medication_plans
                    (
                        id, patient_id, submission_id,
                        medication_name, start_date, end_date
                    )
                VALUES
                    (
                        1, 'demo-patient',
                        '00000000-0000-0000-0000-000000000001',
                        'legacy-a', '2026-04-20', '2026-04-20'
                    ),
                    (
                        2, 'demo-patient', NULL,
                        'legacy-b', '2026-04-20', '2026-04-20'
                    )
                """
            )
        )

    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    run_migrations(engine)
    inspector = inspect(engine)
    columns = {
        column["name"] for column in inspector.get_columns("medication_plans")
    }
    assert "submission_id" not in columns

    with engine.begin() as connection:
        medication_names = connection.execute(
            text(
                "SELECT medication_name "
                "FROM medication_plans ORDER BY id"
            )
        ).scalars().all()

    assert medication_names == ["legacy-a", "legacy-b"]
    cleanup()
