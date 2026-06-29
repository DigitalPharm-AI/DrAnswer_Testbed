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
    (
        "20260618_0001_nutrition_profiles",
        """
        CREATE TABLE IF NOT EXISTS nutrition_profiles (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient' UNIQUE,
            name VARCHAR(120) NOT NULL DEFAULT '데모 환자',
            age INTEGER NOT NULL DEFAULT 55,
            gender VARCHAR(20) NOT NULL DEFAULT 'male',
            height FLOAT NOT NULL DEFAULT 170.0,
            weight FLOAT NOT NULL DEFAULT 70.0,
            disease VARCHAR(80) NOT NULL DEFAULT 'kidney_cancer',
            activity_level VARCHAR(40) NOT NULL DEFAULT 'sedentary',
            egfr FLOAT,
            ckd_stage VARCHAR(20) NOT NULL DEFAULT '',
            ckd_risk VARCHAR(20) NOT NULL DEFAULT 'high',
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
    ),
    (
        "20260618_0002_nutrition_meals",
        """
        CREATE TABLE IF NOT EXISTS nutrition_meals (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
            meal_type VARCHAR(20) NOT NULL,
            meal_date DATE NOT NULL,
            meal_time VARCHAR(8) NOT NULL DEFAULT '',
            scenario_key VARCHAR(80) NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at DATETIME
        )
        """,
    ),
    (
        "20260618_0003_nutrition_foods",
        """
        CREATE TABLE IF NOT EXISTS nutrition_foods (
            id INTEGER NOT NULL PRIMARY KEY,
            meal_id INTEGER NOT NULL,
            food_ref_id VARCHAR(120) NOT NULL DEFAULT '',
            food_name VARCHAR(255) NOT NULL,
            portion VARCHAR(120) NOT NULL DEFAULT '1인분',
            calories FLOAT NOT NULL DEFAULT 0.0,
            protein FLOAT NOT NULL DEFAULT 0.0,
            sodium FLOAT NOT NULL DEFAULT 0.0,
            fat FLOAT NOT NULL DEFAULT 0.0,
            carbohydrates FLOAT NOT NULL DEFAULT 0.0,
            created_at DATETIME,
            FOREIGN KEY(meal_id) REFERENCES nutrition_meals (id)
        )
        """,
    ),
    (
        "20260618_0004_daily_nutrition_checks",
        """
        CREATE TABLE IF NOT EXISTS daily_nutrition_checks (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
            check_date DATE NOT NULL,
            total_meals INTEGER NOT NULL DEFAULT 0,
            threshold_calories FLOAT NOT NULL DEFAULT 0.0,
            threshold_protein FLOAT NOT NULL DEFAULT 0.0,
            threshold_sodium FLOAT NOT NULL DEFAULT 0.0,
            threshold_fat FLOAT NOT NULL DEFAULT 0.0,
            threshold_carbohydrates FLOAT NOT NULL DEFAULT 0.0,
            intake_calories FLOAT NOT NULL DEFAULT 0.0,
            intake_protein FLOAT NOT NULL DEFAULT 0.0,
            intake_sodium FLOAT NOT NULL DEFAULT 0.0,
            intake_fat FLOAT NOT NULL DEFAULT 0.0,
            intake_carbohydrates FLOAT NOT NULL DEFAULT 0.0,
            exceeded_calories BOOLEAN NOT NULL DEFAULT 0,
            exceeded_protein BOOLEAN NOT NULL DEFAULT 0,
            exceeded_sodium BOOLEAN NOT NULL DEFAULT 0,
            exceeded_fat BOOLEAN NOT NULL DEFAULT 0,
            exceeded_carbohydrates BOOLEAN NOT NULL DEFAULT 0,
            excess_calories FLOAT NOT NULL DEFAULT 0.0,
            excess_protein FLOAT NOT NULL DEFAULT 0.0,
            excess_sodium FLOAT NOT NULL DEFAULT 0.0,
            excess_fat FLOAT NOT NULL DEFAULT 0.0,
            excess_carbohydrates FLOAT NOT NULL DEFAULT 0.0,
            checked_at DATETIME,
            UNIQUE(patient_id, check_date)
        )
        """,
    ),
    (
        "20260618_0005_nutrition_meal_index",
        """
        CREATE INDEX IF NOT EXISTS ix_nutrition_meals_patient_date ON nutrition_meals (patient_id, meal_date)
        """,
    ),
    (
        "20260618_0006_nutrition_food_index",
        """
        CREATE INDEX IF NOT EXISTS ix_nutrition_foods_meal_id ON nutrition_foods (meal_id)
        """,
    ),
    (
        "20260618_0007_daily_nutrition_index",
        """
        CREATE INDEX IF NOT EXISTS ix_daily_nutrition_patient_date ON daily_nutrition_checks (patient_id, check_date)
        """,
    ),
    (
        "20260619_0001_agent_run_traces",
        """
        CREATE TABLE IF NOT EXISTS agent_run_traces (
            id INTEGER NOT NULL PRIMARY KEY,
            trace_id VARCHAR(120) NOT NULL UNIQUE,
            request_id VARCHAR(180) NOT NULL DEFAULT '',
            patient_id_hash VARCHAR(64) NOT NULL DEFAULT '',
            workflow_name VARCHAR(120) NOT NULL,
            source_event_type VARCHAR(80) NOT NULL DEFAULT '',
            status VARCHAR(40) NOT NULL DEFAULT 'completed',
            agent_name VARCHAR(120) NOT NULL DEFAULT '',
            decision_type VARCHAR(80) NOT NULL DEFAULT '',
            prompt_version_id VARCHAR(120) NOT NULL DEFAULT '',
            provider VARCHAR(80) NOT NULL DEFAULT '',
            model_tier VARCHAR(40) NOT NULL DEFAULT '',
            model_id VARCHAR(255) NOT NULL DEFAULT '',
            input_hash VARCHAR(64) NOT NULL DEFAULT '',
            output_hash VARCHAR(64) NOT NULL DEFAULT '',
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd FLOAT NOT NULL DEFAULT 0.0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            tool_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            started_at DATETIME,
            completed_at DATETIME,
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
    ),
    (
        "20260619_0002_agent_run_steps",
        """
        CREATE TABLE IF NOT EXISTS agent_run_steps (
            id INTEGER NOT NULL PRIMARY KEY,
            trace_id VARCHAR(120) NOT NULL,
            step_type VARCHAR(60) NOT NULL,
            step_name VARCHAR(120) NOT NULL DEFAULT '',
            status VARCHAR(40) NOT NULL DEFAULT '',
            latency_ms INTEGER NOT NULL DEFAULT 0,
            tool_name VARCHAR(120) NOT NULL DEFAULT '',
            side_effect_level VARCHAR(40) NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME
        )
        """,
    ),
    (
        "20260619_0003_agent_trace_indexes",
        """
        CREATE INDEX IF NOT EXISTS ix_agent_run_traces_patient_workflow ON agent_run_traces (patient_id_hash, workflow_name)
        """,
    ),
    (
        "20260619_0004_agent_run_steps_trace",
        """
        CREATE INDEX IF NOT EXISTS ix_agent_run_steps_trace_id ON agent_run_steps (trace_id)
        """,
    ),
    (
        "20260622_0001_nutrition_ontology_nodes",
        """
        CREATE TABLE IF NOT EXISTS nutrition_ontology_nodes (
            id INTEGER NOT NULL PRIMARY KEY,
            node_key VARCHAR(180) NOT NULL UNIQUE,
            node_type VARCHAR(40) NOT NULL,
            label VARCHAR(255) NOT NULL,
            normalized_label VARCHAR(255) NOT NULL,
            source VARCHAR(80) NOT NULL DEFAULT 'seed',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME,
            updated_at DATETIME
        )
        """,
    ),
    (
        "20260622_0002_nutrition_ontology_triples",
        """
        CREATE TABLE IF NOT EXISTS nutrition_ontology_triples (
            id INTEGER NOT NULL PRIMARY KEY,
            subject_node_id INTEGER NOT NULL,
            predicate VARCHAR(80) NOT NULL,
            object_node_id INTEGER NOT NULL,
            confidence FLOAT NOT NULL DEFAULT 1.0,
            source VARCHAR(80) NOT NULL DEFAULT 'seed',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at DATETIME,
            updated_at DATETIME,
            UNIQUE(subject_node_id, predicate, object_node_id),
            FOREIGN KEY(subject_node_id) REFERENCES nutrition_ontology_nodes (id),
            FOREIGN KEY(object_node_id) REFERENCES nutrition_ontology_nodes (id)
        )
        """,
    ),
    (
        "20260622_0003_nutrition_patient_preference_triples",
        """
        CREATE TABLE IF NOT EXISTS nutrition_patient_preference_triples (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'demo-patient',
            predicate VARCHAR(80) NOT NULL,
            object_node_id INTEGER NOT NULL,
            strength FLOAT NOT NULL DEFAULT 1.0,
            safety_level VARCHAR(20) NOT NULL DEFAULT 'soft',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            source VARCHAR(80) NOT NULL DEFAULT 'agent_tool',
            evidence_text TEXT NOT NULL DEFAULT '',
            source_trace_id VARCHAR(120) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at DATETIME,
            updated_at DATETIME,
            UNIQUE(patient_id, predicate, object_node_id),
            FOREIGN KEY(object_node_id) REFERENCES nutrition_ontology_nodes (id)
        )
        """,
    ),
    (
        "20260622_0004_nutrition_ontology_indexes",
        """
        CREATE INDEX IF NOT EXISTS ix_nutrition_ontology_nodes_type_label ON nutrition_ontology_nodes (node_type, normalized_label)
        """,
    ),
    (
        "20260622_0005_nutrition_patient_preference_indexes",
        """
        CREATE INDEX IF NOT EXISTS ix_nutrition_patient_preferences_lookup
        ON nutrition_patient_preference_triples (patient_id, status, safety_level)
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
