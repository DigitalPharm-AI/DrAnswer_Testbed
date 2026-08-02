from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, inspect, text

from agent_app.persistence.migrations import (
    required_migration_versions,
    run_migrations,
)
from agent_app.persistence.models import Base
from shared.migrations import migration_advisory_lock, migration_status

AGENT_MIGRATION_LOCK_NAMESPACE = "dranswer.agent_internal.schema"
RETIRED_AGENT_TABLES = {
    "agent_conversation_locks",
    "agent_chat_continuations",
}
RETIRED_AGENT_COLUMNS = {
    "agent_run_traces": {"fallback_reason"},
}
MFDS_REFERENCE_TABLES = {
    "mfds_import_runs",
    "mfds_drug_products",
    "mfds_label_documents",
    "mfds_product_label_documents",
    "mfds_label_sections",
    "mfds_document_label_sections",
    "mfds_adverse_reactions",
    "mfds_section_adverse_reactions",
    "mfds_drug_aliases",
    "mfds_adverse_reaction_embeddings",
    "agent_pro_ctcae_alias_embeddings",
    "clinical_symptom_concepts",
    "clinical_symptom_aliases",
    "mfds_reaction_concept_links",
    "mfds_reaction_concept_decisions",
    "agent_symptom_resolution_states",
    "clinical_symptom_concept_link_statuses",
}
MFDS_REFERENCE_VIEWS = {"mfds_product_adverse_reaction_view"}


def migrate_agent_schema(engine: Engine) -> dict[str, Any]:
    """Apply Agent DDL once under a cross-process PostgreSQL lock."""

    with migration_advisory_lock(
        engine,
        AGENT_MIGRATION_LOCK_NAMESPACE,
    ):
        Base.metadata.create_all(bind=engine)
        applied_now = run_migrations(engine)
        status = verify_agent_schema_current(engine)
    return {
        **status,
        "applied_now": applied_now,
    }


def verify_agent_schema_current(engine: Engine) -> dict[str, Any]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            f"agent_database_postgresql_required:{engine.dialect.name}"
        )
    expected_versions = required_migration_versions(engine)
    status = migration_status(engine, expected_versions)
    if not status["current"]:
        raise RuntimeError("agent_database_migrations_incomplete")

    inspector = inspect(engine)
    actual_tables = set(inspector.get_table_names())
    retired_tables = sorted(RETIRED_AGENT_TABLES & actual_tables)
    retired_columns: dict[str, list[str]] = {}
    with engine.connect() as connection:
        columns_by_table: dict[str, set[str]] = {}
        for table_name, column_name in connection.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                """
            )
        ):
            columns_by_table.setdefault(str(table_name), set()).add(
                str(column_name)
            )
        for table_name, columns in columns_by_table.items():
            if "conversation_id" in columns:
                retired_columns[table_name] = ["conversation_id"]
            forbidden = sorted(
                columns & RETIRED_AGENT_COLUMNS.get(table_name, set())
            )
            if forbidden:
                retired_columns.setdefault(table_name, []).extend(
                    forbidden
                )
        retired_object_names = list(
            connection.execute(
                text(
                    """
                    SELECT object_name
                    FROM (
                        SELECT c.relname AS object_name
                        FROM pg_class AS c
                        JOIN pg_namespace AS n
                          ON n.oid = c.relnamespace
                        WHERE n.nspname = current_schema()
                          AND c.relkind IN ('i', 'I')
                        UNION ALL
                        SELECT constraint_name AS object_name
                        FROM information_schema.table_constraints
                        WHERE constraint_schema = current_schema()
                    ) AS schema_objects
                    WHERE lower(object_name) LIKE '%conversation_id%'
                    """
                )
            ).scalars()
        )
    if retired_tables or retired_columns or retired_object_names:
        raise RuntimeError("agent_database_retired_schema_present")

    missing_tables = sorted(
        (set(Base.metadata.tables) | MFDS_REFERENCE_TABLES) - actual_tables
    )
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
        raise RuntimeError("agent_database_schema_incomplete")
    actual_views = set(inspector.get_view_names())
    if MFDS_REFERENCE_VIEWS - actual_views:
        raise RuntimeError("agent_database_schema_incomplete")
    return {
        **status,
        "schema_current": True,
    }
