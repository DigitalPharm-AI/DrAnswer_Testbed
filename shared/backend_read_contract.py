from __future__ import annotations

from typing import Final

BACKEND_READ_CONTRACT_VERSION: Final = "1.4"

# These projections are the only Backend DB objects the AI Server is allowed to
# query. Keep their column order stable: readiness checks compare the live views
# to this contract and fail closed on missing or unexpected columns.
BACKEND_READ_VIEW_DEFINITIONS: Final[dict[str, dict[str, object]]] = {
    "ai_v13_chat_messages": {
        "sources": {
            "chat_messages": (
                "id",
                "public_id",
                "patient_id",
                "role",
                "content",
                "conversation_at",
                "recorded_at",
                "message_type",
                "message_payload_json",
                "reply_to_message_id",
                "metadata_json",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "role",
            "content",
            "conversation_at",
            "recorded_at",
            "conversation_sequence",
            "message_type",
            "message_payload_json",
            "reply_to_message_id",
            "metadata_json",
        ),
        "select": """
            SELECT message.public_id AS id,
                   message.patient_id,
                   message.role,
                   message.content,
                   message.conversation_at,
                   message.recorded_at,
                   message.id AS conversation_sequence,
                   message.message_type,
                   (
                       COALESCE(
                           NULLIF(message.message_payload_json, ''),
                           '{}'
                       )::jsonb
                       - 'conversation_id'
                       - 'conversationId'
                   )::text AS message_payload_json,
                   source.public_id AS reply_to_message_id,
                   (
                       COALESCE(
                           NULLIF(message.metadata_json, ''),
                           '{}'
                       )::jsonb
                       - 'conversation_id'
                       - 'conversationId'
                   )::text AS metadata_json
            FROM chat_messages message
            LEFT JOIN chat_messages source
              ON source.id = message.reply_to_message_id
        """,
    },
    "ai_v13_patient_profiles": {
        "sources": {
            "nutrition_profiles": (
                "patient_id",
                "age",
                "gender",
                "height",
                "weight",
                "disease",
                "activity_level",
                "egfr",
                "ckd_stage",
                "ckd_risk",
                "updated_at",
            ),
        },
        "columns": (
            "patient_id",
            "age",
            "gender",
            "height",
            "weight",
            "disease",
            "activity_level",
            "egfr",
            "ckd_stage",
            "ckd_risk",
            "updated_at",
        ),
        "select": """
            SELECT patient_id, age, gender, height, weight, disease,
                   activity_level, egfr, ckd_stage, ckd_risk, updated_at
            FROM nutrition_profiles
        """,
    },
    "ai_v13_active_medication_schedules": {
        "sources": {
            "medication_plans": (
                "id",
                "patient_id",
                "medication_name",
                "dosage",
                "instructions",
                "treatment_area",
                "start_date",
                "end_date",
                "active",
                "source_type",
                "source_key",
            ),
            "dose_schedules": (
                "plan_id",
                "slot_label",
                "scheduled_time",
            ),
        },
        "columns": (
            "patient_id",
            "medication_name",
            "dosage",
            "instructions",
            "treatment_area",
            "start_date",
            "end_date",
            "slot_label",
            "scheduled_time",
            "source_type",
            "source_key",
        ),
        "select": """
            SELECT p.patient_id, p.medication_name, p.dosage, p.instructions,
                   p.treatment_area, p.start_date, p.end_date,
                   s.slot_label, s.scheduled_time, p.source_type, p.source_key
            FROM medication_plans p
            JOIN dose_schedules s ON s.plan_id = p.id
            WHERE p.active = TRUE
        """,
    },
    "ai_v13_dose_events": {
        "sources": {
            "dose_events": (
                "id",
                "public_id",
                "patient_id",
                "medication_name",
                "slot_label",
                "scheduled_for",
                "status",
                "taken_at",
                "note",
                "version",
            ),
            "agent_jobs": (
                "id",
                "request_id",
                "job_type",
                "payload_json",
                "related_dose_event_id",
                "created_at",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "medication_name",
            "slot_label",
            "scheduled_for",
            "status",
            "taken_at",
            "note",
            "version",
            "missed_dose_request_id",
            "adherence_pattern_context_json",
            "tone_policy_context_json",
        ),
        "select": """
            SELECT event.public_id AS id, event.patient_id,
                   event.medication_name, event.slot_label,
                   event.scheduled_for, event.status, event.taken_at,
                   event.note, event.version,
                   missed_job.request_id AS missed_dose_request_id,
                   COALESCE(
                       (
                           NULLIF(missed_job.payload_json, '')::jsonb
                           -> 'adherence_pattern_context'
                       )::text,
                       '{}'
                   ) AS adherence_pattern_context_json,
                   COALESCE(
                       (
                           NULLIF(missed_job.payload_json, '')::jsonb
                           -> 'tone_policy_context'
                       )::text,
                       '{}'
                   ) AS tone_policy_context_json
            FROM dose_events event
            LEFT JOIN LATERAL (
                SELECT job.request_id, job.payload_json
                FROM agent_jobs job
                WHERE job.related_dose_event_id = event.id
                  AND job.job_type = 'missed_dose'
                ORDER BY job.created_at DESC, job.id DESC
                LIMIT 1
            ) missed_job ON TRUE
        """,
    },
    "ai_v13_nutrition_meals": {
        "sources": {
            "nutrition_meals": (
                "public_id",
                "patient_id",
                "meal_type",
                "meal_date",
                "meal_time",
                "scenario_key",
                "description",
                "version",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "meal_type",
            "meal_date",
            "meal_time",
            "scenario_key",
            "description",
            "version",
        ),
        "select": """
            SELECT public_id AS id, patient_id, meal_type, meal_date, meal_time,
                   scenario_key, description, version
            FROM nutrition_meals
        """,
    },
    "ai_v13_nutrition_foods": {
        "sources": {
            "nutrition_foods": (
                "public_id",
                "meal_id",
                "food_ref_id",
                "food_name",
                "portion",
                "calories",
                "protein",
                "sodium",
                "fat",
                "carbohydrates",
                "version",
            ),
            "nutrition_meals": (
                "id",
                "public_id",
            ),
        },
        "columns": (
            "id",
            "meal_id",
            "food_ref_id",
            "food_name",
            "portion",
            "calories",
            "protein",
            "sodium",
            "fat",
            "carbohydrates",
            "version",
        ),
        "select": """
            SELECT f.public_id AS id, m.public_id AS meal_id, f.food_ref_id,
                   f.food_name, f.portion, f.calories, f.protein, f.sodium,
                   f.fat, f.carbohydrates, f.version
            FROM nutrition_foods f
            JOIN nutrition_meals m ON m.id = f.meal_id
        """,
    },
    "ai_v13_nutrition_food_ref": {
        "sources": {
            "nutrition_food_ref": (
                "food_ref_id",
                "food_name",
                "category",
                "serving_size",
                "energy",
                "carbohydrate",
                "protein",
                "fat",
                "sodium",
                "source",
                "manufacturer",
            ),
        },
        "columns": (
            "food_ref_id",
            "food_name",
            "category",
            "serving_size",
            "energy",
            "carbohydrate",
            "protein",
            "fat",
            "sodium",
            "source",
            "manufacturer",
        ),
        "select": """
            SELECT food_ref_id, food_name, category, serving_size, energy,
                   carbohydrate, protein, fat, sodium, source, manufacturer
            FROM nutrition_food_ref
        """,
    },
    "ai_v13_side_effect_records": {
        "sources": {
            "side_effect_records": (
                "public_id",
                "patient_id",
                "medication_name",
                "symptom_text",
                "symptom_onset_text",
                "suspected",
                "severity_result_json",
                "matched_effects_json",
                "matched_items_json",
                "related_dose_event_id",
                "version",
                "created_at",
                "updated_at",
            ),
            "dose_events": (
                "id",
                "public_id",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "medication_name",
            "symptom_text",
            "symptom_onset_text",
            "suspected",
            "severity_result_json",
            "matched_effects_json",
            "matched_items_json",
            "related_dose_event_id",
            "version",
            "created_at",
            "updated_at",
        ),
        "select": """
            SELECT s.public_id AS id, s.patient_id, s.medication_name,
                   s.symptom_text, s.symptom_onset_text, s.suspected,
                   s.severity_result_json, s.matched_effects_json,
                   s.matched_items_json,
                   d.public_id AS related_dose_event_id,
                   s.version, s.created_at, s.updated_at
            FROM side_effect_records s
            LEFT JOIN dose_events d ON d.id = s.related_dose_event_id
        """,
    },
    "ai_v13_nutrition_preferences": {
        "sources": {
            "nutrition_patient_preference_triples": (
                "patient_id",
                "status",
                "predicate",
                "strength",
                "safety_level",
                "confidence",
                "source",
                "evidence_text",
                "object_node_id",
            ),
            "nutrition_ontology_nodes": (
                "id",
                "node_key",
                "node_type",
                "label",
            ),
        },
        "columns": (
            "patient_id",
            "status",
            "predicate",
            "strength",
            "safety_level",
            "confidence",
            "source",
            "evidence_text",
            "node_key",
            "node_type",
            "label",
        ),
        "select": """
            SELECT p.patient_id, p.status, p.predicate, p.strength, p.safety_level,
                   p.confidence, p.source, p.evidence_text,
                   n.node_key, n.node_type, n.label
            FROM nutrition_patient_preference_triples p
            JOIN nutrition_ontology_nodes n ON n.id = p.object_node_id
        """,
    },
    "ai_v13_reminder_policies": {
        "sources": {
            "reminder_policies": (
                "patient_id",
                "public_id",
                "policy_key",
                "slot_label",
                "extra_reminders",
                "interval_minutes",
                "missed_dose_after_minutes",
                "primary_reminder_timing",
                "primary_reminder_offset_minutes",
                "effective_start_date",
                "effective_end_date",
                "active",
                "version",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "policy_key",
            "slot_label",
            "extra_reminders",
            "interval_minutes",
            "missed_dose_after_minutes",
            "primary_reminder_timing",
            "primary_reminder_offset_minutes",
            "effective_start_date",
            "effective_end_date",
            "active",
            "version",
        ),
        "select": """
            SELECT public_id AS id, patient_id, policy_key, slot_label,
                   extra_reminders, interval_minutes, missed_dose_after_minutes,
                   primary_reminder_timing, primary_reminder_offset_minutes,
                   effective_start_date, effective_end_date, active, version
            FROM reminder_policies
        """,
    },
}

BACKEND_READ_VIEW_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    view_name: tuple(definition["columns"])
    for view_name, definition in BACKEND_READ_VIEW_DEFINITIONS.items()
}

# Values required for patient scoping, public identifiers, and optimistic
# concurrency must never be NULL. Backend migration backfill and AI readiness
# both enforce these live-data invariants.
BACKEND_READ_NON_NULL_INVARIANTS: Final[dict[str, tuple[str, ...]]] = {
    "ai_v13_chat_messages": (
        "id",
        "patient_id",
        "conversation_at",
        "recorded_at",
        "conversation_sequence",
    ),
    "ai_v13_patient_profiles": ("patient_id",),
    "ai_v13_active_medication_schedules": (
        "patient_id",
        "medication_name",
        "start_date",
        "end_date",
        "slot_label",
        "scheduled_time",
    ),
    "ai_v13_dose_events": ("id", "patient_id", "version"),
    "ai_v13_nutrition_meals": ("id", "patient_id", "version"),
    "ai_v13_nutrition_foods": ("id", "meal_id", "version"),
    "ai_v13_nutrition_food_ref": ("food_ref_id",),
    "ai_v13_side_effect_records": ("id", "patient_id", "version"),
    "ai_v13_nutrition_preferences": ("patient_id", "status"),
    "ai_v13_reminder_policies": ("id", "patient_id", "version"),
}
