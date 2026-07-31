from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, inspect

from shared.backend_read_contract import BACKEND_READ_VIEW_COLUMNS
from shared.migrations import migration_advisory_lock, migration_status
from system_app.migrations import required_migration_versions, run_migrations
from system_app.models import Base

SYSTEM_MIGRATION_LOCK_NAMESPACE = "dranswer.backend.schema"
RETIRED_SYSTEM_TABLES = {
    "agent_decision_audits",
    "agent_run_traces",
    "agent_run_steps",
    "agent_tool_executions",
    "phr_patients",
    "phr_item_precautions",
    "phr_patient_medications",
    "phr_side_effect_assessments",
    "simulation_patient_profiles",
    "mutation_confirmations",
}
RETIRED_SYSTEM_COLUMNS = {
    "medication_plans": {"submission_id"},
    "nutrition_patient_preference_triples": {"source_trace_id"},
    "side_effect_records": {"severity", "source_trace_id"},
}
def migrate_system_schema(engine: Engine) -> dict[str, Any]:
    """Apply Backend DDL only from the dedicated migration process."""

    _require_postgresql(engine)
    with migration_advisory_lock(
        engine,
        SYSTEM_MIGRATION_LOCK_NAMESPACE,
    ):
        Base.metadata.create_all(bind=engine)
        applied_now = run_migrations(engine)
        status = verify_system_schema_current(engine)
    return {
        **status,
        "applied_now": applied_now,
    }


def verify_system_schema_current(engine: Engine) -> dict[str, Any]:
    """Fail closed when runtime starts before the Backend migration job."""

    _require_postgresql(engine)
    expected_versions = required_migration_versions()
    status = migration_status(engine, expected_versions)
    if not status["current"]:
        raise RuntimeError("system_database_migrations_incomplete")

    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())
    retired_tables = sorted(RETIRED_SYSTEM_TABLES & actual_tables)
    retired_columns: dict[str, list[str]] = {}
    for table_name in actual_tables:
        columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        if "conversation_id" in columns:
            retired_columns[table_name] = ["conversation_id"]
        forbidden = sorted(
            columns & RETIRED_SYSTEM_COLUMNS.get(table_name, set())
        )
        if forbidden:
            retired_columns.setdefault(table_name, []).extend(forbidden)
    live_views = set(inspector.get_view_names())
    expected_read_views = set(BACKEND_READ_VIEW_COLUMNS)
    retired_views = sorted(
        view_name
        for view_name in live_views
        if view_name.startswith("ai_v")
        and view_name not in expected_read_views
    )
    if retired_tables or retired_columns or retired_views:
        raise RuntimeError("system_database_retired_schema_present")

    missing_tables = sorted(set(Base.metadata.tables) - actual_tables)
    missing_columns: dict[str, list[str]] = {}
    for table_name, table in Base.metadata.tables.items():
        if table_name in missing_tables:
            continue
        actual_columns = {
            str(column["name"])
            for column in inspector.get_columns(table_name)
        }
        missing = sorted(set(table.columns.keys()) - actual_columns)
        if missing:
            missing_columns[table_name] = missing
    if missing_tables or missing_columns:
        raise RuntimeError("system_database_schema_incomplete")

    missing_views = sorted(expected_read_views - live_views)
    mismatched_views: dict[str, dict[str, list[str]]] = {}
    for view_name, expected_columns in BACKEND_READ_VIEW_COLUMNS.items():
        if view_name in missing_views:
            continue
        actual_columns = tuple(
            str(column["name"])
            for column in inspector.get_columns(view_name)
        )
        if actual_columns != expected_columns:
            mismatched_views[view_name] = {
                "expected": list(expected_columns),
                "actual": list(actual_columns),
            }
    if missing_views or mismatched_views:
        raise RuntimeError("system_backend_read_views_incomplete")

    return {
        **status,
        "schema_current": True,
        "read_views_current": True,
    }


def _require_postgresql(engine: Engine) -> None:
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            f"system_database_postgresql_required:{engine.dialect.name}"
        )
