from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DBAPIError

from shared.backend_read_contract import (
    BACKEND_READ_VIEW_COLUMNS,
    BACKEND_READ_VIEW_DEFINITIONS,
)
from shared.migrations import run_sql_migrations, table_columns
from shared.public_ids import PublicIdKind, new_public_id
from shared.retention_policy import AGENT_OBSERVABILITY_RETENTION_DAYS

BACKEND_AUDIT_RETENTION_VERSION = (
    "20260728_0003_backend_audit_retention"
)

MIGRATIONS: list[tuple[str, str]] = [
    (
        "20260429_0001_agent_jobs",
        """
        CREATE TABLE IF NOT EXISTS agent_jobs (
            id INTEGER NOT NULL PRIMARY KEY,
            request_id VARCHAR(32) NOT NULL UNIQUE,
            job_type VARCHAR(40) NOT NULL,
            status VARCHAR(20) NOT NULL,
            payload_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            related_dose_event_id INTEGER,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY(related_dose_event_id) REFERENCES dose_events (id)
        )
        """,
    ),
    (
        "20260518_0001_system_policy_overrides",
        """
        CREATE TABLE IF NOT EXISTS system_policy_overrides (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
            policy_key VARCHAR(120) NOT NULL,
            value VARCHAR(120) NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            source VARCHAR(40) NOT NULL DEFAULT 'patient_request',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """,
    ),
    (
        "20260602_0001_missed_dose_flags",
        """
        CREATE TABLE IF NOT EXISTS missed_dose_flags (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
            flag_date DATE NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            trigger_slot_label VARCHAR(120) NOT NULL DEFAULT '',
            related_dose_event_id INTEGER,
            activated_at TIMESTAMP,
            cleared_at TIMESTAMP,
            clear_reason TEXT NOT NULL DEFAULT '',
            subsequent_taken_count INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
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
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001' UNIQUE,
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
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """,
    ),
    (
        "20260618_0002_nutrition_meals",
        """
        CREATE TABLE IF NOT EXISTS nutrition_meals (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
            meal_type VARCHAR(20) NOT NULL,
            meal_date DATE NOT NULL,
            meal_time VARCHAR(8) NOT NULL DEFAULT '',
            scenario_key VARCHAR(80) NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP
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
            created_at TIMESTAMP,
            FOREIGN KEY(meal_id) REFERENCES nutrition_meals (id)
        )
        """,
    ),
    (
        "20260618_0004_daily_nutrition_checks",
        """
        CREATE TABLE IF NOT EXISTS daily_nutrition_checks (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
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
            exceeded_calories BOOLEAN NOT NULL DEFAULT FALSE,
            exceeded_protein BOOLEAN NOT NULL DEFAULT FALSE,
            exceeded_sodium BOOLEAN NOT NULL DEFAULT FALSE,
            exceeded_fat BOOLEAN NOT NULL DEFAULT FALSE,
            exceeded_carbohydrates BOOLEAN NOT NULL DEFAULT FALSE,
            excess_calories FLOAT NOT NULL DEFAULT 0.0,
            excess_protein FLOAT NOT NULL DEFAULT 0.0,
            excess_sodium FLOAT NOT NULL DEFAULT 0.0,
            excess_fat FLOAT NOT NULL DEFAULT 0.0,
            excess_carbohydrates FLOAT NOT NULL DEFAULT 0.0,
            checked_at TIMESTAMP,
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
            created_at TIMESTAMP,
            updated_at TIMESTAMP
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
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
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
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
            predicate VARCHAR(80) NOT NULL,
            object_node_id INTEGER NOT NULL,
            strength FLOAT NOT NULL DEFAULT 1.0,
            safety_level VARCHAR(20) NOT NULL DEFAULT 'soft',
            confidence FLOAT NOT NULL DEFAULT 1.0,
            source VARCHAR(80) NOT NULL DEFAULT 'agent_tool',
            evidence_text TEXT NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
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
    (
        "20260701_0001_nutrition_food_ref",
        """
        CREATE TABLE IF NOT EXISTS nutrition_food_ref (
            food_ref_id  VARCHAR(120) NOT NULL PRIMARY KEY,
            food_name    VARCHAR(255) NOT NULL,
            category     VARCHAR(120) NOT NULL DEFAULT '',
            serving_size FLOAT,
            energy       FLOAT,
            carbohydrate FLOAT,
            protein      FLOAT,
            fat          FLOAT,
            sodium       FLOAT,
            sugar        FLOAT,
            cholesterol  FLOAT,
            moisture     FLOAT,
            source       VARCHAR(120) NOT NULL DEFAULT '',
            manufacturer VARCHAR(120) NOT NULL DEFAULT ''
        )
        """,
    ),
    (
        "20260701_0002_nutrition_food_ref_index",
        "CREATE INDEX IF NOT EXISTS ix_nutrition_food_ref_name ON nutrition_food_ref (food_name)",
    ),
    (
        "20260710_0001_side_effect_records",
        """
        CREATE TABLE IF NOT EXISTS side_effect_records (
            id INTEGER NOT NULL PRIMARY KEY,
            patient_id VARCHAR(100) NOT NULL DEFAULT 'patient_0000000000000001',
            medication_name VARCHAR(255) NOT NULL DEFAULT '',
            symptom_text TEXT NOT NULL DEFAULT '',
            suspected BOOLEAN NOT NULL DEFAULT FALSE,
            matched_effects_json TEXT NOT NULL DEFAULT '[]',
            matched_items_json TEXT NOT NULL DEFAULT '[]',
            evidence TEXT NOT NULL DEFAULT '',
            recommendation TEXT NOT NULL DEFAULT '',
            source_event_type VARCHAR(80) NOT NULL DEFAULT 'agent_tool',
            related_dose_event_id INTEGER,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TIMESTAMP,
            FOREIGN KEY(related_dose_event_id) REFERENCES dose_events (id)
        )
        """,
    ),
    (
        "20260710_0002_side_effect_records_patient_created",
        "CREATE INDEX IF NOT EXISTS ix_side_effect_records_patient_created ON side_effect_records (patient_id, created_at)",
    ),
    (
        "20260710_0003_side_effect_records_suspected_medication",
        "CREATE INDEX IF NOT EXISTS ix_side_effect_records_suspected_medication ON side_effect_records (suspected, medication_name)",
    ),
    (
        "20260715_0001_mutation_confirmations",
        """
        CREATE TABLE IF NOT EXISTS mutation_confirmations (
            id INTEGER NOT NULL PRIMARY KEY,
            public_id VARCHAR(64) NOT NULL UNIQUE,
            patient_id VARCHAR(100) NOT NULL,
            source_chat_request_id VARCHAR(180) NOT NULL DEFAULT '',
            source_message_id VARCHAR(180) NOT NULL DEFAULT '',
            action_type VARCHAR(40) NOT NULL,
            action_name VARCHAR(160) NOT NULL,
            tool_call_id VARCHAR(180) NOT NULL DEFAULT '',
            arguments_json TEXT NOT NULL DEFAULT '{}',
            action_fingerprint VARCHAR(64) NOT NULL,
            target_snapshot_json TEXT NOT NULL DEFAULT '{}',
            target_snapshot_hash VARCHAR(64) NOT NULL,
            display_json TEXT NOT NULL DEFAULT '{}',
            continuation_json TEXT NOT NULL DEFAULT '{}',
            idempotency_key VARCHAR(255) NOT NULL UNIQUE,
            status VARCHAR(32) NOT NULL DEFAULT 'pending',
            result_json TEXT NOT NULL DEFAULT '{}',
            error_message TEXT NOT NULL DEFAULT '',
            chat_message_id INTEGER,
            execution_started_at TIMESTAMP,
            resolved_at TIMESTAMP,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY(chat_message_id) REFERENCES chat_messages (id)
        )
        """,
    ),
    (
        "20260715_0002_mutation_confirmations_patient_status",
        "CREATE INDEX IF NOT EXISTS ix_mutation_confirmations_patient_status ON mutation_confirmations (patient_id, status)",
    ),
    (
        "20260715_0003_mutation_confirmations_fingerprint",
        "CREATE INDEX IF NOT EXISTS ix_mutation_confirmations_fingerprint ON mutation_confirmations (action_fingerprint)",
    ),
    (
        "20260725_0001_backend_api_requests",
        """
        CREATE TABLE IF NOT EXISTS backend_api_requests (
            id INTEGER NOT NULL PRIMARY KEY,
            api_path VARCHAR(180) NOT NULL,
            request_id VARCHAR(180) NOT NULL,
            request_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'PROCESSING',
            http_status INTEGER NOT NULL DEFAULT 0,
            response_json TEXT NOT NULL DEFAULT '{}',
            error_code VARCHAR(120) NOT NULL DEFAULT '',
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            UNIQUE(api_path, request_id)
        )
        """,
    ),
    (
        "20260725_0002_backend_api_request_lookup",
        """
        CREATE INDEX IF NOT EXISTS ix_backend_api_requests_status_updated
        ON backend_api_requests (status, updated_at)
        """,
    ),
    (
        "20260726_0003_test_medication_scenarios",
        """
        CREATE TABLE IF NOT EXISTS test_medication_scenarios (
            scenario_id VARCHAR(80) NOT NULL PRIMARY KEY,
            name VARCHAR(120) NOT NULL,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """,
    ),
    (
        "20260726_0004_test_medication_scenario_items",
        """
        CREATE TABLE IF NOT EXISTS test_medication_scenario_items (
            id INTEGER NOT NULL PRIMARY KEY,
            scenario_id VARCHAR(80) NOT NULL,
            medication_id VARCHAR(80) NOT NULL,
            medication_name VARCHAR(255) NOT NULL,
            dosage VARCHAR(255) NOT NULL DEFAULT '',
            treatment_area VARCHAR(80) NOT NULL,
            slot_label VARCHAR(120) NOT NULL,
            scheduled_time VARCHAR(8) NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP,
            UNIQUE(scenario_id, medication_id),
            FOREIGN KEY(scenario_id) REFERENCES test_medication_scenarios (scenario_id)
        )
        """,
    ),
    (
        "20260726_0005_test_medication_scenario_item_lookup",
        """
        CREATE INDEX IF NOT EXISTS ix_test_medication_scenario_items_scenario
        ON test_medication_scenario_items (scenario_id, sort_order)
        """,
    ),
    (
        "20260727_0002_agent_async_callback_receipts",
        """
        CREATE TABLE IF NOT EXISTS agent_async_callback_receipts (
            request_id VARCHAR(180) NOT NULL PRIMARY KEY,
            callback_hash VARCHAR(64) NOT NULL,
            event_type VARCHAR(60) NOT NULL,
            result_status VARCHAR(24) NOT NULL,
            processed_at TIMESTAMP
        )
        """,
    ),
    (
        "20260728_0004_notification_policy_change_proposals",
        """
        CREATE TABLE IF NOT EXISTS notification_policy_change_proposals (
            request_id VARCHAR(180) NOT NULL PRIMARY KEY,
            callback_hash VARCHAR(64) NOT NULL,
            patient_id VARCHAR(100) NOT NULL,
            proposed_policy_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'pending_user_confirmation',
            notification_id INTEGER UNIQUE,
            created_at TIMESTAMP,
            updated_at TIMESTAMP,
            FOREIGN KEY(notification_id) REFERENCES notifications (id)
        );
        CREATE INDEX IF NOT EXISTS
            ix_notification_policy_change_proposals_patient_id
        ON notification_policy_change_proposals (patient_id);
        CREATE INDEX IF NOT EXISTS
            ix_notification_policy_change_proposals_status
        ON notification_policy_change_proposals (status)
        """,
    ),
    (
        "20260728_0005_patient_id_defaults",
        """
        ALTER TABLE medication_plans
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_profiles
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_meals
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE daily_nutrition_checks
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_patient_preference_triples
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE dose_events
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE side_effect_records
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE missed_dose_flags
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE reminder_policies
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE system_policy_overrides
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE notifications
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE chat_messages
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001'
        """,
    ),
    (
        "20260729_0001_side_effect_contract",
        """
        ALTER TABLE side_effect_records
            ADD COLUMN IF NOT EXISTS symptom_onset_text
            TEXT NOT NULL DEFAULT '';
        ALTER TABLE side_effect_records
            ADD COLUMN IF NOT EXISTS severity_result_json
            TEXT NOT NULL DEFAULT '{"questions":[],"responses":[]}'
        """,
    ),
    (
        "20260729_0003_v13_public_id_defaults",
        """
        ALTER TABLE medication_plans
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_profiles
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_meals
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE daily_nutrition_checks
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE nutrition_patient_preference_triples
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE dose_events
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE side_effect_records
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE missed_dose_flags
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE reminder_policies
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE system_policy_overrides
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE notifications
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001';
        ALTER TABLE chat_messages
            ALTER COLUMN patient_id SET DEFAULT 'patient_0000000000000001'
        """,
    ),
    (
        "20260729_0004_backend_trace_single_source_cleanup",
        """
        ALTER TABLE mutation_confirmations
            ADD COLUMN IF NOT EXISTS source_chat_request_id
            VARCHAR(180) NOT NULL DEFAULT '';
        ALTER TABLE mutation_confirmations
            ADD COLUMN IF NOT EXISTS source_message_id
            VARCHAR(180) NOT NULL DEFAULT '';

        UPDATE mutation_confirmations
        SET source_chat_request_id = split_part(idempotency_key, ':', 1)
        WHERE source_chat_request_id = '';
        UPDATE mutation_confirmations
        SET source_message_id = split_part(idempotency_key, ':', 2)
        WHERE source_message_id = '';

        CREATE INDEX IF NOT EXISTS
            ix_mutation_confirmations_source_chat_request_id
        ON mutation_confirmations (source_chat_request_id);
        CREATE INDEX IF NOT EXISTS
            ix_mutation_confirmations_source_message_id
        ON mutation_confirmations (source_message_id);

        ALTER TABLE mutation_confirmations
            DROP COLUMN IF EXISTS origin_request_notification_id;
        ALTER TABLE mutation_confirmations
            DROP COLUMN IF EXISTS origin_trace_id;
        ALTER TABLE mutation_confirmations
            DROP COLUMN IF EXISTS origin_agent;
        ALTER TABLE mutation_confirmations
            DROP COLUMN IF EXISTS source_event_type;

        ALTER TABLE nutrition_patient_preference_triples
            DROP COLUMN IF EXISTS source_trace_id;

        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'side_effect_records'
                  AND column_name = 'severity'
            ) THEN
                UPDATE side_effect_records
                SET metadata_json = (
                    COALESCE(NULLIF(metadata_json, ''), '{}')::jsonb
                    || jsonb_build_object('legacy_severity', severity)
                )::text
                WHERE severity IS NOT NULL
                  AND severity NOT IN ('', 'none', 'pro_ctcae');
            END IF;
        END
        $$;
        ALTER TABLE side_effect_records
            DROP COLUMN IF EXISTS severity;
        ALTER TABLE side_effect_records
            DROP COLUMN IF EXISTS source_trace_id;

        ALTER TABLE medication_plans
            DROP CONSTRAINT IF EXISTS uq_medication_plan_patient_submission;
        DROP INDEX IF EXISTS uq_medication_plan_patient_submission;
        ALTER TABLE medication_plans
            DROP COLUMN IF EXISTS submission_id;

        DROP TABLE IF EXISTS agent_decision_audits
        """,
    ),
    (
        "20260729_0005_remove_backend_trace_metadata",
        """
        UPDATE notifications
        SET metadata_json = (
            COALESCE(NULLIF(metadata_json, ''), '{}')::jsonb
            - 'trace_id'
        )::text
        WHERE (
            COALESCE(NULLIF(metadata_json, ''), '{}')::jsonb
            ? 'trace_id'
        )
        """,
    ),
    (
        "20260730_0006_v13_backend_read_views",
        """
        DO $$
        DECLARE
            view_suffix TEXT;
            old_view_name TEXT;
            new_view_name TEXT;
            old_view REGCLASS;
            new_view REGCLASS;
        BEGIN
            FOREACH view_suffix IN ARRAY ARRAY[
                'active_medication_schedules',
                'chat_messages',
                'dose_events',
                'nutrition_food_ref',
                'nutrition_foods',
                'nutrition_meals',
                'nutrition_preferences',
                'patient_profiles',
                'reminder_policies',
                'side_effect_records'
            ]
            LOOP
                old_view_name := 'ai_v12_' || view_suffix;
                new_view_name := 'ai_v13_' || view_suffix;
                old_view := to_regclass(
                    format('%I.%I', current_schema(), old_view_name)
                );
                new_view := to_regclass(
                    format('%I.%I', current_schema(), new_view_name)
                );

                IF old_view IS NOT NULL AND new_view IS NOT NULL THEN
                    RAISE EXCEPTION
                        'backend_read_view_versions_overlap:%',
                        view_suffix;
                ELSIF old_view IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER VIEW %I.%I RENAME TO %I',
                        current_schema(),
                        old_view_name,
                        new_view_name
                    );
                END IF;
            END LOOP;
        END
        $$;
        """,
    ),
    (
        "20260730_0007_remove_backend_mutation_confirmations",
        "DROP TABLE IF EXISTS mutation_confirmations",
    ),
    (
        "20260731_0001_chat_time_axes",
        """
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS display_at
            TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS conversation_at TIMESTAMP;
        ALTER TABLE chat_messages
            ADD COLUMN IF NOT EXISTS recorded_at
            TIMESTAMP DEFAULT CURRENT_TIMESTAMP;

        UPDATE chat_messages
        SET conversation_at = COALESCE(
                conversation_at,
                display_at,
                created_at,
                CURRENT_TIMESTAMP
            ),
            recorded_at = COALESCE(
                created_at,
                recorded_at,
                CURRENT_TIMESTAMP
            );

        ALTER TABLE chat_messages
            ALTER COLUMN conversation_at SET NOT NULL;
        ALTER TABLE chat_messages
            ALTER COLUMN recorded_at SET NOT NULL;

        CREATE INDEX IF NOT EXISTS
            ix_chat_messages_patient_conversation_at
        ON chat_messages (patient_id, conversation_at, id);
        CREATE INDEX IF NOT EXISTS
            ix_chat_messages_patient_sequence
        ON chat_messages (patient_id, id)
        """,
    ),
]


REMINDER_POLICY_COLUMNS: dict[str, str] = {
    "public_id": "VARCHAR(80)",
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
    "version": "INTEGER DEFAULT 1",
    "updated_at": "TIMESTAMP",
}


CHAT_MESSAGE_COLUMNS: dict[str, str] = {
    "ai_request_id": "VARCHAR(180) DEFAULT ''",
    "message_type": "VARCHAR(32) DEFAULT 'text'",
    "message_payload_json": "TEXT DEFAULT '{}'",
    "reply_to_message_id": "INTEGER",
    "processing_status": "VARCHAR(32) DEFAULT 'completed'",
    "metadata_json": "TEXT DEFAULT '{}'",
    "display_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "conversation_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "recorded_at": "TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
}


VERSIONED_TABLE_COLUMNS: dict[str, dict[str, str]] = {
    "dose_events": {
        "version": "INTEGER DEFAULT 1",
        "updated_at": "TIMESTAMP",
    },
    "nutrition_meals": {
        "version": "INTEGER DEFAULT 1",
        "updated_at": "TIMESTAMP",
    },
    "nutrition_foods": {
        "version": "INTEGER DEFAULT 1",
        "updated_at": "TIMESTAMP",
    },
    "side_effect_records": {
        "version": "INTEGER DEFAULT 1",
        "updated_at": "TIMESTAMP",
        "symptom_onset_text": "TEXT DEFAULT ''",
        "severity_result_json": (
            "TEXT DEFAULT '{\"questions\":[],\"responses\":[]}'"
        ),
    },
}

UI_MEDICATION_PLAN_COLUMNS: dict[str, str] = {
    "treatment_area": "VARCHAR(80) DEFAULT ''",
    "source_type": "VARCHAR(40) DEFAULT 'manual'",
    "source_key": "VARCHAR(120) DEFAULT ''",
}

EXTERNAL_PUBLIC_ID_TABLES: dict[str, str] = {
    "chat_messages": "msg",
    "dose_events": "dose",
    "nutrition_meals": "meal",
    "nutrition_foods": "food",
    "side_effect_records": "sidefx",
    "notifications": "notif",
}


def _allocate_migration_public_id(
    connection,
    *,
    table_name: str,
    column_name: str,
    kind: PublicIdKind,
    attempts: int = 8,
) -> str:
    for _attempt in range(attempts):
        candidate = new_public_id(kind)
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended(:candidate, 0))"
                ),
                {"candidate": candidate},
            )
        exists = connection.execute(
            text(
                f"SELECT 1 FROM {table_name} "
                f"WHERE {column_name} = :candidate LIMIT 1"
            ),
            {"candidate": candidate},
        ).first()
        if exists is None:
            return candidate
    raise RuntimeError(
        f"public_id_collision_retry_exhausted:{table_name}:{kind}"
    )


def ensure_reminder_policy_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        existing_columns = table_columns(connection, "reminder_policies")
        if not existing_columns:
            return
        for column_name, column_type in REMINDER_POLICY_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(f"ALTER TABLE reminder_policies ADD COLUMN {column_name} {column_type}"))
        missing_public_ids = connection.execute(
            text("SELECT id FROM reminder_policies WHERE public_id IS NULL OR public_id = ''")
        ).scalars().all()
        for policy_id in missing_public_ids:
            connection.execute(
                text("UPDATE reminder_policies SET public_id = :public_id WHERE id = :policy_id"),
                {
                    "public_id": _allocate_migration_public_id(
                        connection,
                        table_name="reminder_policies",
                        column_name="public_id",
                        kind="notification_policy",
                    ),
                    "policy_id": policy_id,
                },
            )
        connection.execute(
            text(
                "UPDATE reminder_policies "
                "SET version = COALESCE(version, 1), "
                "updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_policies_public_id "
                "ON reminder_policies (public_id)"
            )
        )
        if connection.dialect.name == "postgresql":
            connection.execute(text("ALTER TABLE reminder_policies ALTER COLUMN public_id SET NOT NULL"))
            connection.execute(text("ALTER TABLE reminder_policies ALTER COLUMN version SET NOT NULL"))


def ensure_chat_message_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        existing_columns = table_columns(connection, "chat_messages")
        if not existing_columns:
            return
        for column_name, column_type in CHAT_MESSAGE_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(text(f"ALTER TABLE chat_messages ADD COLUMN {column_name} {column_type}"))
        connection.execute(
            text(
                "UPDATE chat_messages "
                "SET display_at = COALESCE(display_at, created_at, CURRENT_TIMESTAMP), "
                "conversation_at = COALESCE(conversation_at, display_at, created_at, CURRENT_TIMESTAMP), "
                "recorded_at = COALESCE(recorded_at, created_at, CURRENT_TIMESTAMP) "
                "WHERE display_at IS NULL "
                "OR conversation_at IS NULL "
                "OR recorded_at IS NULL"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_chat_messages_patient_display_at "
                "ON chat_messages (patient_id, display_at, id)"
            )
        )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_chat_messages_ai_request_role "
                "ON chat_messages (ai_request_id, role) "
                "WHERE ai_request_id <> ''"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_chat_messages_patient_conversation_at "
                "ON chat_messages (patient_id, conversation_at, id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_chat_messages_patient_sequence "
                "ON chat_messages (patient_id, id)"
            )
        )
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE chat_messages "
                    "ALTER COLUMN display_at SET NOT NULL"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE chat_messages "
                    "ALTER COLUMN conversation_at SET NOT NULL"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE chat_messages "
                    "ALTER COLUMN recorded_at SET NOT NULL"
                )
            )
def ensure_agent_job_request_ids(engine: Engine) -> None:
    """Ensure every async job has the public callback request key."""

    with engine.begin() as connection:
        existing_columns = table_columns(connection, "agent_jobs")
        if not existing_columns:
            return
        if "request_id" not in existing_columns:
            connection.execute(
                text(
                    "ALTER TABLE agent_jobs "
                    "ADD COLUMN request_id VARCHAR(32)"
                )
            )
        missing_rows = connection.execute(
            text(
                "SELECT id FROM agent_jobs "
                "WHERE request_id IS NULL OR request_id = ''"
            )
        ).scalars().all()
        for job_id in missing_rows:
            connection.execute(
                text(
                    "UPDATE agent_jobs SET request_id = :request_id "
                    "WHERE id = :job_id"
                ),
                {
                    "request_id": _allocate_migration_public_id(
                        connection,
                        table_name="agent_jobs",
                        column_name="request_id",
                        kind="request",
                    ),
                    "job_id": job_id,
                },
            )
        connection.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_agent_jobs_request_id ON agent_jobs (request_id)"
            )
        )
        if connection.dialect.name == "postgresql":
            connection.execute(
                text(
                    "ALTER TABLE agent_jobs "
                    "ALTER COLUMN request_id SET NOT NULL"
                )
            )


def ensure_ui_medication_plan_columns(engine: Engine) -> None:
    """Add Backend-owned metadata used by the React testbed read model."""

    with engine.begin() as connection:
        existing_columns = table_columns(connection, "medication_plans")
        if not existing_columns:
            return
        for column_name, column_type in UI_MEDICATION_PLAN_COLUMNS.items():
            if column_name in existing_columns:
                continue
            connection.execute(
                text(f"ALTER TABLE medication_plans ADD COLUMN {column_name} {column_type}")
            )
        connection.execute(
            text(
                "UPDATE medication_plans "
                "SET treatment_area = COALESCE(treatment_area, ''), "
                "source_type = COALESCE(NULLIF(source_type, ''), 'manual'), "
                "source_key = COALESCE(source_key, '')"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_medication_plans_ui_source "
                "ON medication_plans (patient_id, source_type, start_date, end_date)"
            )
        )


def ensure_external_public_ids(engine: Engine) -> None:
    """Add and backfill opaque API identifiers without replacing internal PKs."""

    with engine.begin() as connection:
        for table_name, default_prefix in EXTERNAL_PUBLIC_ID_TABLES.items():
            existing_columns = table_columns(connection, table_name)
            if not existing_columns:
                continue
            if "public_id" not in existing_columns:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN public_id VARCHAR(80)"))

            missing_rows = connection.execute(
                text(f"SELECT id{', role' if table_name == 'chat_messages' else ''} "
                     f"FROM {table_name} WHERE public_id IS NULL OR public_id = ''")
            ).mappings().all()
            for row in missing_rows:
                if table_name == "chat_messages":
                    role = str(row.get("role") or "").strip().lower()
                    if role == "user":
                        kind: PublicIdKind = "user_message"
                    elif role == "assistant":
                        kind = "assistant_message"
                    else:
                        raise RuntimeError(
                            "chat_message_public_id_role_invalid:"
                            f"{row['id']}:{role}"
                        )
                    public_id = _allocate_migration_public_id(
                        connection,
                        table_name=table_name,
                        column_name="public_id",
                        kind=kind,
                    )
                elif table_name == "dose_events":
                    public_id = _allocate_migration_public_id(
                        connection,
                        table_name=table_name,
                        column_name="public_id",
                        kind="dose_event",
                    )
                elif table_name == "nutrition_meals":
                    public_id = _allocate_migration_public_id(
                        connection,
                        table_name=table_name,
                        column_name="public_id",
                        kind="meal",
                    )
                elif table_name == "nutrition_foods":
                    public_id = _allocate_migration_public_id(
                        connection,
                        table_name=table_name,
                        column_name="public_id",
                        kind="food",
                    )
                elif table_name == "side_effect_records":
                    public_id = _allocate_migration_public_id(
                        connection,
                        table_name=table_name,
                        column_name="public_id",
                        kind="side_effect",
                    )
                else:
                    public_id = f"{default_prefix}_{uuid4().hex}"
                connection.execute(
                    text(f"UPDATE {table_name} SET public_id = :public_id WHERE id = :record_id"),
                    {
                        "public_id": public_id,
                        "record_id": row["id"],
                    },
                )

            connection.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS uq_{table_name}_public_id "
                    f"ON {table_name} (public_id)"
                )
            )
            if connection.dialect.name == "postgresql":
                connection.execute(text(f"ALTER TABLE {table_name} ALTER COLUMN public_id SET NOT NULL"))


def ensure_versioned_table_columns(engine: Engine) -> None:
    with engine.begin() as connection:
        for table_name, required_columns in VERSIONED_TABLE_COLUMNS.items():
            existing_columns = table_columns(connection, table_name)
            if not existing_columns:
                continue
            for column_name, column_type in required_columns.items():
                if column_name in existing_columns:
                    continue
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"))
            connection.execute(
                text(
                    f"UPDATE {table_name} "
                    "SET version = COALESCE(version, 1), "
                    "updated_at = COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)"
                )
            )
            if connection.dialect.name == "postgresql":
                connection.execute(text(f"ALTER TABLE {table_name} ALTER COLUMN version SET NOT NULL"))


def ensure_backend_read_views(engine: Engine) -> bool:
    """Create the versioned, least-privilege projections consumed by AI Server.

    The views are managed on every Backend startup so compatible projection
    changes do not depend on an AI Server deployment. If the core Backend
    tables do not exist yet (for example a migration-only unit test), no
    partial contract is published. Once every source table exists, any source
    column drift is a startup error rather than a silently degraded query.
    """

    with engine.begin() as connection:
        missing_tables: set[str] = set()
        missing_columns: dict[str, set[str]] = {}
        source_requirements: dict[str, set[str]] = {}
        for definition in BACKEND_READ_VIEW_DEFINITIONS.values():
            for table_name, columns in dict(definition["sources"]).items():
                source_requirements.setdefault(str(table_name), set()).update(str(column) for column in columns)

        for table_name, required_columns in source_requirements.items():
            actual_columns = table_columns(connection, table_name)
            if not actual_columns:
                missing_tables.add(table_name)
                continue
            difference = required_columns - actual_columns
            if difference:
                missing_columns[table_name] = difference

        if missing_tables:
            _drop_backend_read_views(connection)
            return False
        if missing_columns:
            details = ";".join(
                f"{table_name}:{','.join(sorted(columns))}"
                for table_name, columns in sorted(missing_columns.items())
            )
            raise RuntimeError(f"backend_read_source_schema_mismatch:{details}")

        if connection.dialect.name != "postgresql":
            raise RuntimeError(
                "backend_read_contract_postgresql_required:"
                f"{connection.dialect.name}"
            )
        live_views = set(inspect(connection).get_view_names())
        for view_name, definition in BACKEND_READ_VIEW_DEFINITIONS.items():
            _create_or_replace_backend_read_view(
                connection,
                view_name=view_name,
                select_sql=str(definition["select"]),
                view_exists=view_name in live_views,
            )
    return True


def _create_or_replace_backend_read_view(
    connection,
    *,
    view_name: str,
    select_sql: str,
    view_exists: bool,
) -> None:
    """Repair contract drift without leaving a partially published view."""

    select_grantees = (
        _backend_read_view_select_grantees(connection, view_name)
        if view_exists
        else ()
    )
    create_sql = (
        f"CREATE OR REPLACE VIEW {view_name} AS {select_sql}"
    )
    if view_exists:
        actual_columns = tuple(
            str(column["name"])
            for column in inspect(connection).get_columns(view_name)
        )
        if actual_columns != BACKEND_READ_VIEW_COLUMNS[view_name]:
            connection.execute(
                text(f"DROP VIEW IF EXISTS {view_name}")
            )
            connection.execute(
                text(f"CREATE VIEW {view_name} AS {select_sql}")
            )
            _restore_backend_read_view_select_grants(
                connection,
                view_name,
                select_grantees,
            )
            return

    try:
        # PostgreSQL rejects CREATE OR REPLACE when a column keeps its name
        # but changes type. A savepoint lets us recover that specific drift
        # without aborting the outer all-views transaction.
        with connection.begin_nested():
            connection.execute(text(create_sql))
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", "") != "42P16":
            raise
        connection.execute(text(f"DROP VIEW IF EXISTS {view_name}"))
        connection.execute(
            text(f"CREATE VIEW {view_name} AS {select_sql}")
        )
        _restore_backend_read_view_select_grants(
            connection,
            view_name,
            select_grantees,
        )


def _backend_read_view_select_grantees(
    connection,
    view_name: str,
) -> tuple[str, ...]:
    rows = connection.execute(
        text(
            """
            SELECT DISTINCT grantee
            FROM information_schema.role_table_grants
            WHERE table_schema = current_schema()
              AND table_name = :view_name
              AND privilege_type = 'SELECT'
              AND grantee <> 'PUBLIC'
            ORDER BY grantee
            """
        ),
        {"view_name": view_name},
    ).scalars().all()
    return tuple(str(value) for value in rows if str(value).strip())


def _restore_backend_read_view_select_grants(
    connection,
    view_name: str,
    grantees: tuple[str, ...],
) -> None:
    for grantee in grantees:
        # Both identifiers originate from the migration's fixed view catalog
        # or PostgreSQL's own privilege catalog, never from an API request.
        quoted_grantee = '"' + grantee.replace('"', '""') + '"'
        connection.execute(
            text(
                f"GRANT SELECT ON {view_name} TO {quoted_grantee}"
            )
        )


def _drop_backend_read_views(connection) -> None:
    for view_name in reversed(BACKEND_READ_VIEW_DEFINITIONS):
        connection.execute(text(f"DROP VIEW IF EXISTS {view_name}"))


def ensure_backend_audit_retention(engine: Engine) -> bool:
    """Backfill a fixed three-year expiry for minimal Backend audit rows."""

    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            "backend_audit_retention_postgresql_required:"
            f"{engine.dialect.name}"
        )
    audit_tables = {
        "agent_async_callback_receipts": "processed_at",
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(80) NOT NULL PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        already_applied = connection.scalar(
            text(
                "SELECT 1 FROM schema_migrations "
                "WHERE version = :version"
            ),
            {"version": BACKEND_AUDIT_RETENTION_VERSION},
        )
        if already_applied:
            return False

        table_names = set(inspect(connection).get_table_names())
        for table_name, recorded_at_column in audit_tables.items():
            if table_name not in table_names:
                continue
            existing_columns = table_columns(connection, table_name)
            if "expires_at" not in existing_columns:
                connection.execute(
                    text(
                        f"ALTER TABLE {table_name} "
                        "ADD COLUMN expires_at TIMESTAMP"
                    )
                )
            connection.execute(
                text(
                    f"UPDATE {table_name} "
                    "SET expires_at = COALESCE("
                    f"{recorded_at_column}, CURRENT_TIMESTAMP"
                    f") + INTERVAL '{AGENT_OBSERVABILITY_RETENTION_DAYS} days' "
                    "WHERE expires_at IS NULL"
                )
            )
            connection.execute(
                text(
                    f"ALTER TABLE {table_name} "
                    "ALTER COLUMN expires_at SET NOT NULL"
                )
            )
            connection.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS "
                    f"ix_{table_name}_expires_at "
                    f"ON {table_name} (expires_at)"
                )
            )
        connection.execute(
            text(
                "INSERT INTO schema_migrations (version) "
                "VALUES (:version)"
            ),
            {"version": BACKEND_AUDIT_RETENTION_VERSION},
        )
    return True


def run_migrations(engine: Engine) -> list[str]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            f"system_migration_postgresql_required:{engine.dialect.name}"
        )
    applied = run_sql_migrations(engine, MIGRATIONS)
    ensure_reminder_policy_columns(engine)
    ensure_chat_message_columns(engine)
    ensure_agent_job_request_ids(engine)
    ensure_ui_medication_plan_columns(engine)
    ensure_versioned_table_columns(engine)
    ensure_external_public_ids(engine)
    if ensure_backend_audit_retention(engine):
        applied.append(BACKEND_AUDIT_RETENTION_VERSION)
    ensure_backend_read_views(engine)
    return applied


def required_migration_versions() -> tuple[str, ...]:
    return tuple(version for version, _sql in MIGRATIONS) + (
        BACKEND_AUDIT_RETENTION_VERSION,
    )
