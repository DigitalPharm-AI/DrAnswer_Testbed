from __future__ import annotations

from sqlalchemy import Engine, text

from shared.migrations import run_sql_migrations, table_columns

MIGRATIONS: list[tuple[str, str]] = [
    (
        "20260429_0001_agent_jobs",
        """
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id INTEGER NOT NULL PRIMARY KEY,
            job_type VARCHAR(40) NOT NULL,
            status VARCHAR(20) NOT NULL,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            related_dose_event_id INTEGER,
            created_at DATETIME,
            updated_at DATETIME,
            started_at DATETIME,
            completed_at DATETIME,
            FOREIGN KEY(related_dose_event_id) REFERENCES dose_events (id)
        )
        """,
    ),
    (
        "20260430_0001_simulation_patient_profiles",
        """
        CREATE TABLE IF NOT EXISTS simulation_patient_profiles (
            id INTEGER NOT NULL PRIMARY KEY,
            local_patient_id VARCHAR(100) NOT NULL UNIQUE,
            phr_patient_key VARCHAR(160) NOT NULL DEFAULT '',
            sync_status VARCHAR(40) NOT NULL DEFAULT 'unregistered',
            error_message TEXT NOT NULL DEFAULT '',
            registered_at DATETIME,
            updated_at DATETIME,
            created_at DATETIME
        )
        """,
    ),
    (
        "20260518_0001_system_policy_overrides",
        """
        CREATE TABLE IF NOT EXISTS system_policy_overrides (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
            policy_key VARCHAR(120) NOT NULL,
            value VARCHAR(120) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source VARCHAR(40) NOT NULL DEFAULT 'patient_request',
            active BOOLEAN NOT NULL DEFAULT 1,
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
    ),
    (
        "20260602_0001_missed_dose_flags",
        """
        CREATE TABLE IF NOT EXISTS missed_dose_flags (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
            flag_date DATE NOT NULL,
            active BOOLEAN NOT NULL DEFAULT 1,
            trigger_slot_label VARCHAR(120) NOT NULL DEFAULT '',
            related_dose_event_id INTEGER,
            activated_at DATETIME,
            cleared_at DATETIME,
            clear_reason TEXT NOT NULL DEFAULT '',
            subsequent_taken_count INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME,
            updated_at DATETIME,
            UNIQUE(patient_id, flag_date),
            FOREIGN KEY(related_dose_event_id) REFERENCES dose_events (id)
        )
        """,
    ),
]


REMINDER_POLICY_COLUMNS: dict[str, str] = {
    "policy_key": "VARCHAR(120) DEFAULT 'custom'",
    "missed_dose_after_minutes": "INTEGER",
    "primary_reminder_timing": "VARCHAR(20) DEFAULT 'at'",
    "primary_reminder_offset_minutes": "INTEGER DEFAULT 0",
    "medication_title_template": "TEXT DEFAULT '{medication_name} 복약 알림'",
    "medication_body_template": "TEXT DEFAULT '{slot_label} 복약 시간입니다. 복약 후 기록 버튼을 눌러주세요.'",
    "extra_title_template": "TEXT DEFAULT '{medication_name} 추가 복약 알림'",
    "extra_body_template": "TEXT DEFAULT '{slot_label} 복약을 놓치지 않도록 다시 알려드려요.'",
    "missed_dose_title_template": "TEXT DEFAULT '미복용 AI 알림'",
    "missed_dose_body_template": (
        "TEXT DEFAULT '{slot_label} {medication_name} 미복용이 확정되어 AI가 상황을 확인하고 있어요.'"
    ),
    "updated_at": "DATETIME",
}


CHAT_MESSAGE_COLUMNS: dict[str, str] = {
    "metadata_json": "TEXT DEFAULT '{}'",
}


def ensure_reminder_policy_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        existing_columns = table_columns(connection, "reminder_policies")
        if not existing_columns:
            return
        for column_name, column_type in REMINDER_POLICY_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(f"ALTER TABLE reminder_policies ADD COLUMN {column_name} {column_type}"))


def ensure_chat_message_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        existing_columns = table_columns(connection, "chat_messages")
        if not existing_columns:
            return
        for column_name, column_type in CHAT_MESSAGE_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(f"ALTER TABLE chat_messages ADD COLUMN {column_name} {column_type}"))


def run_migrations(engine: Engine) -> list[str]:
    applied = run_sql_migrations(engine, MIGRATIONS)
    ensure_reminder_policy_columns(engine)
    ensure_chat_message_columns(engine)
    return applied
