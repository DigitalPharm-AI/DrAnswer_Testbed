from __future__ import annotations

import inspect as python_inspect
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from agent_app.tools.backend_query import BackendQueryTools, BackendReadContractError
from shared.backend_read_contract import (
    BACKEND_READ_CONTRACT_VERSION,
    BACKEND_READ_VIEW_COLUMNS,
)
from system_app.migrations import run_migrations
from system_app.models import Base


def _database(tmp_path: Path, name: str = "backend-read-contract.db"):
    database_path = (tmp_path / name).as_posix()
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    return database_path, engine


def test_backend_migration_publishes_exact_versioned_views_and_repairs_drift(
    tmp_path: Path,
) -> None:
    database_path, engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v12_chat_messages"))
        connection.execute(
            text(
                "CREATE VIEW ai_v12_chat_messages AS "
                "SELECT id, patient_id FROM chat_messages"
            )
        )

    assert run_migrations(engine) == []

    inspector = inspect(engine)
    assert set(BACKEND_READ_VIEW_COLUMNS) <= set(inspector.get_view_names())
    for view_name, expected_columns in BACKEND_READ_VIEW_COLUMNS.items():
        assert tuple(
            str(column["name"]) for column in inspector.get_columns(view_name)
        ) == expected_columns

    queries = BackendQueryTools(f"sqlite:///{database_path}")
    assert queries.verify_contract() == {
        "ok": True,
        "contract_version": BACKEND_READ_CONTRACT_VERSION,
        "dialect": "sqlite",
        "read_only": True,
        "views": sorted(BACKEND_READ_VIEW_COLUMNS),
    }
    queries.engine.dispose()
    engine.dispose()


def test_backend_query_startup_fails_closed_for_missing_or_drifted_view(
    tmp_path: Path,
) -> None:
    database_path, engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v12_chat_messages"))

    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_view_missing:ai_v12_chat_messages",
    ):
        BackendQueryTools(f"sqlite:///{database_path}")

    run_migrations(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v12_chat_messages"))
        connection.execute(
            text(
                "CREATE VIEW ai_v12_chat_messages AS "
                "SELECT id, patient_id FROM chat_messages"
            )
        )
    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_columns_mismatch:ai_v12_chat_messages",
    ):
        BackendQueryTools(f"sqlite:///{database_path}")
    engine.dispose()


def test_backend_query_startup_rejects_null_scope_or_version_invariant(
    tmp_path: Path,
) -> None:
    database_path, engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO dose_events (
                    id, public_id, patient_id, plan_id, schedule_id, medication_name,
                    slot_label, scheduled_for, status, alerts_generated,
                    missed_handled, note, version, created_at, updated_at
                ) VALUES (
                    701, 'dose_701', 'patient-contract', 1, 1, 'test-medication',
                    'morning', CURRENT_TIMESTAMP, 'scheduled', 0,
                    0, '', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(text("DROP VIEW ai_v12_dose_events"))
        connection.execute(
            text(
                """
                CREATE VIEW ai_v12_dose_events AS
                SELECT id, patient_id, medication_name, slot_label, scheduled_for,
                       status, taken_at, note, NULL AS version
                FROM dose_events
                """
            )
        )

    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_null_invariant:ai_v12_dose_events:version",
    ):
        BackendQueryTools(f"sqlite:///{database_path}")
    engine.dispose()


def test_legacy_nullable_public_id_and_versions_are_backfilled(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "legacy-backfill.db").as_posix()
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE reminder_policies (
                    id INTEGER PRIMARY KEY,
                    public_id VARCHAR(80),
                    patient_id VARCHAR(100) NOT NULL,
                    policy_key VARCHAR(120),
                    slot_label VARCHAR(120) NOT NULL,
                    extra_reminders INTEGER NOT NULL,
                    interval_minutes INTEGER NOT NULL,
                    missed_dose_after_minutes INTEGER,
                    primary_reminder_timing VARCHAR(20),
                    primary_reminder_offset_minutes INTEGER,
                    effective_start_date DATE NOT NULL,
                    effective_end_date DATE NOT NULL,
                    active BOOLEAN NOT NULL,
                    version INTEGER,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO reminder_policies (
                    id, public_id, patient_id, policy_key, slot_label,
                    extra_reminders, interval_minutes, effective_start_date,
                    effective_end_date, active, version, created_at
                ) VALUES (
                    1, NULL, 'legacy-patient', 'legacy', 'morning',
                    1, 15, '2026-07-25', '2026-07-26', 1, NULL,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )
    Base.metadata.create_all(engine)
    run_migrations(engine)

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT public_id, version "
                "FROM ai_v12_reminder_policies WHERE id = 1"
            )
        ).mappings().one()
    assert str(row["public_id"]).startswith("npol_")
    assert row["version"] == 1

    queries = BackendQueryTools(f"sqlite:///{database_path}")
    assert queries.verify_contract()["ok"] is True
    queries.engine.dispose()
    engine.dispose()


def test_machine_readable_schema_matches_runtime_contract() -> None:
    schema_path = Path("docs/BACKEND_V12_READ_SCHEMA.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["contract_version"] == BACKEND_READ_CONTRACT_VERSION
    assert {
        view_name: tuple(view["columns"])
        for view_name, view in schema["views"].items()
    } == BACKEND_READ_VIEW_COLUMNS
    assert schema["access"]["allowed_operations"] == ["SELECT"]
    assert schema["data_handling"]["ai_server_persistence"].startswith(
        "Query rows and raw patient content must not be persisted"
    )


def test_backend_query_implementation_has_no_raw_backend_table_reads() -> None:
    source = python_inspect.getsource(BackendQueryTools)
    forbidden_fragments = (
        "FROM chat_messages",
        "FROM dose_events",
        "FROM nutrition_meals",
        "FROM nutrition_foods",
        "FROM nutrition_food_ref",
        "FROM side_effect_records",
        "FROM nutrition_patient_preference_triples",
        "FROM reminder_policies",
        "JOIN nutrition_meals",
        "JOIN nutrition_ontology_nodes",
    )
    assert all(fragment not in source for fragment in forbidden_fragments)
