from __future__ import annotations

import inspect as python_inspect
import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from agent_app.tools.backend_query import BackendQueryTools, BackendReadContractError
from shared.backend_read_contract import (
    BACKEND_READ_CONTRACT_VERSION,
    BACKEND_READ_VIEW_COLUMNS,
    BACKEND_READ_VIEW_DEFINITIONS,
)
from system_app.migrations import (
    ensure_backend_read_views,
    run_migrations,
)
from system_app.models import Base
from tests.helpers import (
    build_backend_reader_url,
    build_system_engine,
)


def _database(tmp_path: Path, name: str = "backend_read_contract"):
    engine, _cleanup = build_system_engine("backend_read_contract")
    run_migrations(engine)
    return engine


def test_backend_migration_publishes_exact_versioned_views_and_repairs_drift(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v13_chat_messages"))
        connection.execute(
            text(
                "CREATE VIEW ai_v13_chat_messages AS "
                "SELECT id, patient_id, role, content, created_at, "
                "message_type, metadata_json FROM chat_messages"
            )
        )
    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_contract_reader",
    )

    assert run_migrations(engine) == []

    inspector = inspect(engine)
    assert set(BACKEND_READ_VIEW_COLUMNS) <= set(inspector.get_view_names())
    for view_name, expected_columns in BACKEND_READ_VIEW_COLUMNS.items():
        assert tuple(
            str(column["name"]) for column in inspector.get_columns(view_name)
        ) == expected_columns

    queries = BackendQueryTools(reader_url)
    contract = queries.verify_contract()
    assert contract["server_version"]
    assert {
        key: value
        for key, value in contract.items()
        if key != "server_version"
    } == {
        "ok": True,
        "contract_version": BACKEND_READ_CONTRACT_VERSION,
        "dialect": "postgresql",
        "read_only": True,
        "views": sorted(BACKEND_READ_VIEW_COLUMNS),
    }
    queries.engine.dispose()
    engine.dispose()


def test_backend_view_drift_repair_rolls_back_on_invalid_definition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v13_chat_messages"))
        connection.execute(
            text(
                "CREATE VIEW ai_v13_chat_messages AS "
                "SELECT id, patient_id, role, content, created_at, "
                "message_type, metadata_json FROM chat_messages"
            )
        )

    definition = BACKEND_READ_VIEW_DEFINITIONS[
        "ai_v13_chat_messages"
    ]
    monkeypatch.setitem(
        definition,
        "select",
        "SELECT missing_contract_column FROM chat_messages",
    )
    with pytest.raises(SQLAlchemyError):
        ensure_backend_read_views(engine)

    columns = inspect(engine).get_columns("ai_v13_chat_messages")
    assert tuple(column["name"] for column in columns) == (
        "id",
        "patient_id",
        "role",
        "content",
        "created_at",
        "message_type",
        "metadata_json",
    )
    assert str(columns[0]["type"]).upper() == "INTEGER"


def test_backend_query_startup_fails_closed_for_missing_or_drifted_view(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    reader_url, reader_cleanup = build_backend_reader_url(
        engine,
        "backend_missing_view_reader",
    )
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v13_chat_messages"))

    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_view_missing:ai_v13_chat_messages",
    ):
        BackendQueryTools(reader_url)

    reader_cleanup()
    run_migrations(engine)
    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_drifted_view_reader",
    )
    with engine.begin() as connection:
        connection.execute(text("DROP VIEW ai_v13_chat_messages"))
        connection.execute(
            text(
                "CREATE VIEW ai_v13_chat_messages AS "
                "SELECT id, patient_id FROM chat_messages"
            )
        )
    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_columns_mismatch:ai_v13_chat_messages",
    ):
        BackendQueryTools(reader_url)
    engine.dispose()


def test_backend_query_startup_rejects_null_scope_or_version_invariant(
    tmp_path: Path,
) -> None:
    engine = _database(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO medication_plans (
                    id, patient_id, medication_name, dosage,
                    instructions, treatment_area, source_type,
                    source_key, start_date, end_date, active,
                    created_at
                ) VALUES (
                    1, 'patient-contract', 'test-medication', '',
                    '', '', 'test', 'contract',
                    '2026-07-25', '2026-07-25', TRUE,
                    CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO dose_schedules (
                    id, plan_id, slot_label, scheduled_time, created_at
                ) VALUES (
                    1, 1, 'morning', '08:00', CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO dose_events (
                    id, public_id, patient_id, plan_id, schedule_id, medication_name,
                    slot_label, scheduled_for, status, alerts_generated,
                    missed_handled, note, version, created_at, updated_at
                ) VALUES (
                    701, 'dose_701', 'patient-contract', 1, 1, 'test-medication',
                    'morning', CURRENT_TIMESTAMP, 'scheduled', FALSE,
                    FALSE, '', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            )
        )
        connection.execute(text("DROP VIEW ai_v13_dose_events"))
        connection.execute(
            text(
                """
                CREATE VIEW ai_v13_dose_events AS
                SELECT id, patient_id, medication_name, slot_label, scheduled_for,
                       status, taken_at, note, NULL AS version,
                       NULL::varchar AS missed_dose_request_id,
                       '{}'::text AS adherence_pattern_context_json,
                       '{}'::text AS tone_policy_context_json
                FROM dose_events
                """
            )
        )

    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_null_invariant_reader",
    )
    with pytest.raises(
        BackendReadContractError,
        match="backend_read_contract_null_invariant:ai_v13_dose_events:version",
    ):
        BackendQueryTools(reader_url)
    engine.dispose()


def test_legacy_nullable_public_id_and_versions_are_backfilled(
    tmp_path: Path,
) -> None:
    engine, _cleanup = build_system_engine(
        "backend_legacy_backfill",
        create_models=False,
    )
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
                    created_at TIMESTAMP,
                    updated_at TIMESTAMP
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
                    1, 15, '2026-07-25', '2026-07-26', TRUE, NULL,
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
                "SELECT id, version "
                "FROM ai_v13_reminder_policies "
                "WHERE patient_id = 'legacy-patient'"
            )
        ).mappings().one()
    assert str(row["id"]).startswith("npol_")
    assert row["version"] == 1

    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_legacy_reader",
    )
    queries = BackendQueryTools(reader_url)
    assert queries.verify_contract()["ok"] is True
    queries.engine.dispose()
    engine.dispose()


def test_machine_readable_schema_matches_runtime_contract() -> None:
    schema_path = Path("docs/BACKEND_V13_READ_SCHEMA.json")
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


def test_food_candidate_read_orders_by_relevance_before_limit(
    tmp_path: Path,
) -> None:
    engine = _database(
        tmp_path,
        "food_search_order",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO nutrition_food_ref (
                    food_ref_id, food_name, category, source, manufacturer
                ) VALUES
                    ('food-substring', '오징어_짜장', '', '', ''),
                    ('food-long-prefix', '짜장밥_오징어', '', '', ''),
                    ('food-short-prefix', '짜장면', '', '', '')
                """
            )
        )

    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_food_reader",
    )
    queries = BackendQueryTools(reader_url)
    result = queries.search_food_candidates(query="짜장", limit=1)

    assert result["candidates"][0]["food_name"] == "짜장면"
    queries.engine.dispose()
    engine.dispose()


def test_food_candidate_read_expands_compound_name_inside_tool(
    tmp_path: Path,
) -> None:
    engine = _database(
        tmp_path,
        "food_search_expansion",
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO nutrition_food_ref (
                    food_ref_id, food_name, category, source, manufacturer
                ) VALUES
                    ('food-tofu-soup', '두부국', '', '', ''),
                    ('food-beef-tofu-soup', '소고기 두부국', '', '', ''),
                    ('food-unrelated', '초콜릿 케이크', '', '', '')
                """
            )
        )

    reader_url, _reader_cleanup = build_backend_reader_url(
        engine,
        "backend_food_expansion_reader",
    )
    queries = BackendQueryTools(reader_url)
    result = queries.search_food_candidates(
        query="두부된장국",
        limit=6,
    )

    assert result["match_mode"] == "expanded"
    assert [item["food_name"] for item in result["candidates"]] == [
        "두부국",
        "소고기 두부국",
    ]
    queries.engine.dispose()
    engine.dispose()


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
