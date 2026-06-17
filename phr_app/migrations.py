from __future__ import annotations

from sqlalchemy import Engine

from shared.migrations import run_sql_migrations, table_columns

MIGRATIONS: list[tuple[str, str]] = [
    (
        "20260430_0001_patient_key_precautions",
        """
        CREATE TABLE IF NOT EXISTS phr_patients (
            id INTEGER NOT NULL PRIMARY KEY,
            phr_patient_key VARCHAR(160) NOT NULL UNIQUE,
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME
        )
        """,
    ),
    (
        "20260430_0002_item_precautions",
        """
        CREATE TABLE IF NOT EXISTS phr_item_precautions (
            id INTEGER NOT NULL PRIMARY KEY,
            item_name VARCHAR(255) NOT NULL UNIQUE,
            precautions_text TEXT NOT NULL DEFAULT '',
            severity_hint VARCHAR(40) NOT NULL DEFAULT 'moderate',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME
        )
        """,
    ),
    (
        "20260430_0003_patient_medication_key_columns",
        """
        ALTER TABLE phr_patient_medications ADD COLUMN phr_patient_key VARCHAR(160) NOT NULL DEFAULT ''
        """,
    ),
    (
        "20260430_0004_patient_medication_item_column",
        """
        ALTER TABLE phr_patient_medications ADD COLUMN item_name VARCHAR(255) NOT NULL DEFAULT ''
        """,
    ),
    (
        "20260430_0005_assessment_key_column",
        """
        ALTER TABLE phr_side_effect_assessments ADD COLUMN phr_patient_key VARCHAR(160) NOT NULL DEFAULT ''
        """,
    ),
    (
        "20260430_0006_assessment_items_column",
        """
        ALTER TABLE phr_side_effect_assessments ADD COLUMN matched_items_json TEXT NOT NULL DEFAULT '[]'
        """,
    ),
    (
        "20260430_0007_assessment_precautions_column",
        """
        ALTER TABLE phr_side_effect_assessments ADD COLUMN matched_precautions_json TEXT NOT NULL DEFAULT '[]'
        """,
    ),
]


def _should_skip(connection, version: str) -> bool:
    if version == "20260430_0003_patient_medication_key_columns":
        return "phr_patient_key" in table_columns(connection, "phr_patient_medications")
    if version == "20260430_0004_patient_medication_item_column":
        return "item_name" in table_columns(connection, "phr_patient_medications")
    if version == "20260430_0005_assessment_key_column":
        return "phr_patient_key" in table_columns(connection, "phr_side_effect_assessments")
    if version == "20260430_0006_assessment_items_column":
        return "matched_items_json" in table_columns(connection, "phr_side_effect_assessments")
    if version == "20260430_0007_assessment_precautions_column":
        return "matched_precautions_json" in table_columns(connection, "phr_side_effect_assessments")
    return False


def run_migrations(engine: Engine) -> list[str]:
    return run_sql_migrations(engine, MIGRATIONS, _should_skip)
