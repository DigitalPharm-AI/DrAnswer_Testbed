from __future__ import annotations

from typing import Final

BACKEND_READ_CONTRACT_VERSION: Final = "1.2"

# These projections are the only Backend DB objects the AI Server is allowed to
# query. Keep their column order stable: readiness checks compare the live views
# to this contract and fail closed on missing or unexpected columns.
BACKEND_READ_VIEW_DEFINITIONS: Final[dict[str, dict[str, object]]] = {
    "ai_v12_chat_messages": {
        "sources": {
            "chat_messages": (
                "public_id",
                "patient_id",
                "conversation_id",
                "role",
                "content",
                "created_at",
                "message_type",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "conversation_id",
            "role",
            "content",
            "created_at",
            "message_type",
        ),
        "select": """
            SELECT public_id AS id, patient_id, conversation_id, role, content,
                   created_at, message_type
            FROM chat_messages
        """,
    },
    "ai_v12_dose_events": {
        "sources": {
            "dose_events": (
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
        ),
        "select": """
            SELECT public_id AS id, patient_id, medication_name, slot_label, scheduled_for,
                   status, taken_at, note, version
            FROM dose_events
        """,
    },
    "ai_v12_nutrition_meals": {
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
    "ai_v12_nutrition_foods": {
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
    "ai_v12_nutrition_food_ref": {
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
    "ai_v12_side_effect_records": {
        "sources": {
            "side_effect_records": (
                "id",
                "patient_id",
                "phr_patient_key",
                "medication_name",
                "symptom_text",
                "suspected",
                "severity",
                "matched_effects_json",
                "matched_items_json",
                "evidence",
                "recommendation",
                "source_event_type",
                "related_dose_event_id",
                "created_at",
            ),
            "dose_events": (
                "id",
                "public_id",
            ),
        },
        "columns": (
            "id",
            "patient_id",
            "phr_patient_key",
            "medication_name",
            "symptom_text",
            "suspected",
            "severity",
            "matched_effects_json",
            "matched_items_json",
            "evidence",
            "recommendation",
            "source_event_type",
            "related_dose_event_id",
            "created_at",
        ),
        "select": """
            SELECT s.id, s.patient_id, s.phr_patient_key, s.medication_name,
                   s.symptom_text, s.suspected, s.severity, s.matched_effects_json,
                   s.matched_items_json, s.evidence, s.recommendation,
                   s.source_event_type, d.public_id AS related_dose_event_id,
                   s.created_at
            FROM side_effect_records s
            LEFT JOIN dose_events d ON d.id = s.related_dose_event_id
        """,
    },
    "ai_v12_nutrition_preferences": {
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
    "ai_v12_reminder_policies": {
        "sources": {
            "reminder_policies": (
                "id",
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
        "select": """
            SELECT id, patient_id, public_id, policy_key, slot_label,
                   extra_reminders, interval_minutes, missed_dose_after_minutes,
                   primary_reminder_timing, primary_reminder_offset_minutes,
                   effective_start_date, effective_end_date, active, version
            FROM reminder_policies
        """,
    },
    # Compatibility-only lookup for in-flight v1.2 requests that still carry
    # the former numeric IDs. Tool code resolves through this view, then uses
    # only public IDs in subsequent queries and responses.
    "ai_v12_legacy_id_map": {
        "sources": {
            "chat_messages": ("id", "public_id"),
            "dose_events": ("id", "public_id"),
            "nutrition_meals": ("id", "public_id"),
            "nutrition_foods": ("id", "public_id"),
        },
        "columns": (
            "entity_type",
            "legacy_id",
            "public_id",
        ),
        "select": """
            SELECT 'message' AS entity_type, CAST(id AS VARCHAR(80)) AS legacy_id,
                   public_id
            FROM chat_messages
            UNION ALL
            SELECT 'dose_event' AS entity_type, CAST(id AS VARCHAR(80)) AS legacy_id,
                   public_id
            FROM dose_events
            UNION ALL
            SELECT 'meal' AS entity_type, CAST(id AS VARCHAR(80)) AS legacy_id,
                   public_id
            FROM nutrition_meals
            UNION ALL
            SELECT 'food' AS entity_type, CAST(id AS VARCHAR(80)) AS legacy_id,
                   public_id
            FROM nutrition_foods
        """,
    },
}

BACKEND_READ_VIEW_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    view_name: tuple(definition["columns"])
    for view_name, definition in BACKEND_READ_VIEW_DEFINITIONS.items()
}

# Values required for patient scoping, public identifiers, and optimistic
# concurrency must never be NULL. SQLite cannot safely add NOT NULL to an
# existing column in place, so both Backend migration backfill and AI readiness
# enforce these live-data invariants.
BACKEND_READ_NON_NULL_INVARIANTS: Final[dict[str, tuple[str, ...]]] = {
    "ai_v12_chat_messages": ("id", "patient_id", "conversation_id"),
    "ai_v12_dose_events": ("id", "patient_id", "version"),
    "ai_v12_nutrition_meals": ("id", "patient_id", "version"),
    "ai_v12_nutrition_foods": ("id", "meal_id", "version"),
    "ai_v12_nutrition_food_ref": ("food_ref_id",),
    "ai_v12_side_effect_records": ("id", "patient_id"),
    "ai_v12_nutrition_preferences": ("patient_id", "status"),
    "ai_v12_reminder_policies": ("id", "patient_id", "public_id", "version"),
    "ai_v12_legacy_id_map": ("entity_type", "legacy_id", "public_id"),
}
