from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.engine import make_url

from agent_app.integration.chat_contracts import ChatSyncRequest
from agent_app.integration.contracts import (
    POLICY_MAX_EFFECTIVE_DAYS,
    POLICY_MAX_EXTRA_REMINDERS,
    POLICY_MAX_INTERVAL_MINUTES,
    POLICY_MAX_MISSED_DOSE_AFTER_MINUTES,
    POLICY_MAX_PRIMARY_REMINDER_OFFSET_MINUTES,
    POLICY_MIN_EXTRA_REMINDERS,
    POLICY_MIN_INTERVAL_MINUTES,
    POLICY_MIN_MISSED_DOSE_AFTER_MINUTES,
    POLICY_MIN_PRIMARY_REMINDER_OFFSET_MINUTES,
)
from shared.backend_read_contract import (
    BACKEND_READ_CONTRACT_VERSION,
    BACKEND_READ_NON_NULL_INVARIANTS,
    BACKEND_READ_VIEW_COLUMNS,
)
from shared.settings import Settings, get_settings


class BackendQueryError(RuntimeError):
    pass


class BackendChatMessageNotFound(BackendQueryError):
    pass


class BackendRecordNotFound(BackendQueryError):
    pass


class BackendReadContractError(BackendQueryError):
    pass


class BackendQueryTools:
    """Fixed, parameterized Backend DB reads. No caller-supplied SQL is accepted."""

    def __init__(self, database_url: str, *, max_rows: int = 100) -> None:
        if not database_url.strip():
            raise ValueError("backend_read_database_url_required")
        self.engine = _read_only_engine(database_url)
        self.max_rows = max(1, min(int(max_rows), 500))
        try:
            self.verify_contract()
        except Exception:
            self.engine.dispose()
            raise

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> BackendQueryTools | None:
        resolved = settings or get_settings()
        if not resolved.backend_read_database_url.strip():
            return None
        return cls(
            resolved.backend_read_database_url,
            max_rows=resolved.backend_query_max_rows,
        )

    def verify_contract(self) -> dict[str, Any]:
        """Verify the live view schema and DB-enforced read-only session."""

        try:
            with self.engine.connect() as connection:
                dialect = connection.dialect.name
                if dialect == "sqlite":
                    read_only = int(connection.exec_driver_sql("PRAGMA query_only").scalar_one()) == 1
                elif dialect == "postgresql":
                    value = connection.execute(text("SHOW transaction_read_only")).scalar_one()
                    read_only = str(value).strip().lower() in {"on", "true", "1"}
                else:
                    raise BackendReadContractError(
                        f"backend_read_contract_unsupported_dialect:{dialect}"
                    )
                if not read_only:
                    raise BackendReadContractError("backend_read_connection_not_read_only")

                inspector = inspect(connection)
                live_views = set(inspector.get_view_names())
                for view_name, expected_columns in BACKEND_READ_VIEW_COLUMNS.items():
                    if view_name not in live_views:
                        raise BackendReadContractError(
                            f"backend_read_contract_view_missing:{view_name}"
                        )
                    actual_columns = tuple(
                        str(column["name"])
                        for column in inspector.get_columns(view_name)
                    )
                    if actual_columns != expected_columns:
                        raise BackendReadContractError(
                            "backend_read_contract_columns_mismatch:"
                            f"{view_name}:expected={','.join(expected_columns)}:"
                            f"actual={','.join(actual_columns)}"
                        )
                    connection.execute(text(f"SELECT * FROM {view_name} WHERE 1 = 0"))
                    for column_name in BACKEND_READ_NON_NULL_INVARIANTS[view_name]:
                        invalid = connection.execute(
                            text(
                                f"SELECT 1 FROM {view_name} "
                                f"WHERE {column_name} IS NULL LIMIT 1"
                            )
                        ).scalar_one_or_none()
                        if invalid is not None:
                            raise BackendReadContractError(
                                "backend_read_contract_null_invariant:"
                                f"{view_name}:{column_name}"
                            )
        except BackendReadContractError:
            raise
        except Exception as exc:
            raise BackendReadContractError(
                f"backend_read_contract_probe_failed:{type(exc).__name__}"
            ) from exc
        return {
            "ok": True,
            "contract_version": BACKEND_READ_CONTRACT_VERSION,
            "dialect": dialect,
            "read_only": True,
            "views": sorted(BACKEND_READ_VIEW_COLUMNS),
        }

    def validate_chat_message(self, request: ChatSyncRequest, *, history_limit: int = 20) -> dict[str, Any]:
        limit = self._limit(history_limit)
        with self.engine.connect() as connection:
            resolved = _resolve_external_id(connection, "message", request.message_id)
            if resolved is None:
                raise BackendChatMessageNotFound("backend_chat_message_not_found")
            message_id, legacy_message_id = resolved
            message = connection.execute(
                text(
                    """
                    SELECT id, patient_id, conversation_id, role, content, created_at
                    FROM ai_v12_chat_messages
                    WHERE id = :message_id
                      AND patient_id = :patient_id
                      AND conversation_id = :conversation_id
                      AND role = 'user'
                    """
                ),
                {
                    "message_id": message_id,
                    "patient_id": request.patient_id,
                    "conversation_id": request.conversation_id,
                },
            ).mappings().first()
            if message is None or str(message["content"]) != request.message:
                raise BackendChatMessageNotFound("backend_chat_message_not_found")
            rows = connection.execute(
                text(
                    """
                    SELECT c.id, c.role, c.message_type, c.content, c.created_at
                    FROM ai_v12_chat_messages c
                    JOIN ai_v12_legacy_id_map legacy
                      ON legacy.entity_type = 'message'
                     AND legacy.public_id = c.id
                    WHERE c.patient_id = :patient_id
                      AND c.conversation_id = :conversation_id
                      AND CAST(legacy.legacy_id AS INTEGER) <= :legacy_message_id
                    ORDER BY CAST(legacy.legacy_id AS INTEGER) DESC
                    LIMIT :limit
                    """
                ),
                {
                    "patient_id": request.patient_id,
                    "conversation_id": request.conversation_id,
                    "legacy_message_id": int(legacy_message_id),
                    "limit": limit,
                },
            ).mappings().all()
        return {
            "backend_message_verified": True,
            "recent_chat": [
                {
                    "message_id": str(row["id"]),
                    "role": str(row["role"]),
                    "message_type": str(row["message_type"] or "text"),
                    "content": str(row["content"]),
                    "created_at": _iso(row["created_at"]),
                }
                for row in reversed(rows)
            ],
        }

    def validate_feedback_target(
        self,
        *,
        message_id: str,
        conversation_id: str,
        patient_id: str,
    ) -> dict[str, Any]:
        """Resolve feedback only to an assistant message in the trusted chat scope."""

        with self.engine.connect() as connection:
            resolved = _resolve_external_id(connection, "message", message_id)
            if resolved is None:
                raise BackendChatMessageNotFound("backend_chat_message_not_found")
            public_id, _legacy_id = resolved
            row = connection.execute(
                text(
                    """
                    SELECT id, conversation_id, patient_id, role
                    FROM ai_v12_chat_messages
                    WHERE id = :message_id
                      AND patient_id = :patient_id
                      AND conversation_id = :conversation_id
                      AND role = 'assistant'
                    """
                ),
                {
                    "message_id": public_id,
                    "patient_id": patient_id,
                    "conversation_id": conversation_id,
                },
            ).mappings().first()
        if row is None:
            raise BackendChatMessageNotFound("backend_chat_message_not_found")
        return {
            "message_id": str(row["id"]),
            "conversation_id": str(row["conversation_id"]),
            "patient_id": str(row["patient_id"]),
            "role": str(row["role"]),
        }

    def medication_dose_status(
        self,
        *,
        patient_id: str,
        target_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | None = None,
        medication_name: str | None = None,
    ) -> dict[str, Any]:
        start, end, target = _date_range(target_date, start_date, end_date, max_days=31)
        clauses = [
            "patient_id = :patient_id",
            "scheduled_for >= :start_at",
            "scheduled_for < :end_exclusive",
        ]
        params: dict[str, Any] = {
            "patient_id": patient_id,
            "start_at": datetime.combine(start, time.min),
            "end_exclusive": datetime.combine(end + timedelta(days=1), time.min),
            "limit": self.max_rows,
        }
        if status:
            clauses.append("status = :status")
            params["status"] = status
        if medication_name:
            clauses.append("LOWER(medication_name) LIKE :medication_name")
            params["medication_name"] = f"%{medication_name.lower()}%"
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT id, patient_id, medication_name, slot_label, scheduled_for,
                           status, taken_at, note, version
                    FROM ai_v12_dose_events
                    WHERE {' AND '.join(clauses)}
                    ORDER BY scheduled_for, id
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        events = [
            {
                "dose_event_id": row["id"],
                "patient_id": row["patient_id"],
                "medication_name": row["medication_name"],
                "slot_label": row["slot_label"],
                "scheduled_for": _iso(row["scheduled_for"]),
                "status": row["status"],
                "taken_at": _iso(row["taken_at"]) if row["taken_at"] else None,
                "note": row["note"] or "",
                "version": row["version"] or 1,
            }
            for row in rows
        ]
        totals = Counter(str(row["status"]) for row in rows)
        by_date: dict[str, Counter[str]] = defaultdict(Counter)
        for row in rows:
            by_date[_as_date(row["scheduled_for"]).isoformat()][str(row["status"])] += 1
        return {
            "success": True,
            "patient_id": patient_id,
            "target_date": target.isoformat() if target else None,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "dose_events": events,
            "total": len(events),
            "summary_by_date": [
                {"date": key, "totals_by_status": dict(value), "total": sum(value.values())}
                for key, value in sorted(by_date.items())
            ],
            "totals_by_status": dict(totals),
        }

    def nutrition_meals(self, *, patient_id: str, meal_date: str | None = None) -> dict[str, Any]:
        clauses = ["m.patient_id = :patient_id"]
        params: dict[str, Any] = {"patient_id": patient_id, "limit": self.max_rows}
        if meal_date:
            clauses.append("m.meal_date = :meal_date")
            params["meal_date"] = date.fromisoformat(meal_date)
        with self.engine.connect() as connection:
            meals = connection.execute(
                text(
                    f"""
                    SELECT m.id, m.patient_id, m.meal_type, m.meal_date, m.meal_time,
                           m.scenario_key, m.description, m.version
                    FROM ai_v12_nutrition_meals m
                    WHERE {' AND '.join(clauses)}
                    ORDER BY m.meal_date DESC, m.meal_time, m.id
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
            meal_ids = [str(row["id"]) for row in meals]
            foods = []
            if meal_ids:
                placeholders = ", ".join(f":meal_{index}" for index in range(len(meal_ids)))
                food_params = {f"meal_{index}": meal_id for index, meal_id in enumerate(meal_ids)}
                foods = connection.execute(
                    text(
                        f"""
                        SELECT id, meal_id, food_ref_id, food_name, portion, calories,
                               protein, sodium, fat, carbohydrates, version
                        FROM ai_v12_nutrition_foods
                        WHERE meal_id IN ({placeholders})
                        ORDER BY meal_id, id
                        """
                    ),
                    food_params,
                ).mappings().all()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in foods:
            grouped[str(row["meal_id"])].append(_food_view(row))
        payload = [
            {
                "id": row["id"],
                "patient_id": row["patient_id"],
                "meal_type": row["meal_type"],
                "meal_date": _as_date(row["meal_date"]).isoformat(),
                "meal_time": row["meal_time"],
                "scenario_key": row["scenario_key"] or "",
                "description": row["description"] or "",
                "version": row["version"] or 1,
                "foods": grouped[str(row["id"])],
            }
            for row in meals
        ]
        return {"success": True, "meals": payload, "total": len(payload)}

    def search_food_candidates(self, *, query: str, limit: int = 6) -> dict[str, Any]:
        normalized = query.strip().lower()
        if not normalized:
            return {"success": False, "error": "query_required", "candidates": [], "source": "db"}
        bounded_limit = self._limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT food_ref_id, food_name, category, serving_size, energy,
                           carbohydrate, protein, fat, sodium, source, manufacturer
                    FROM ai_v12_nutrition_food_ref
                    WHERE LOWER(food_name) LIKE :query
                    ORDER BY CASE WHEN LOWER(food_name) = :exact THEN 0 ELSE 1 END,
                             food_name
                    LIMIT :limit
                    """
                ),
                {"query": f"%{normalized}%", "exact": normalized, "limit": bounded_limit},
            ).mappings().all()
        return {
            "success": True,
            "candidates": [_food_ref_view(row) for row in rows],
            "source": "backend_read_db",
            "error": "",
            "query": query,
            "meal_type": "",
            "limit": bounded_limit,
        }

    def side_effect_history(
        self,
        *,
        patient_id: str,
        limit: int = 20,
        suspected: bool | None = None,
        target_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        medication_name: str | None = None,
        severity: str | None = None,
    ) -> dict[str, Any]:
        clauses = ["patient_id = :patient_id"]
        params: dict[str, Any] = {"patient_id": patient_id, "limit": self._limit(limit)}
        if target_date or start_date or end_date:
            start, end, _ = _date_range(target_date, start_date, end_date, max_days=366)
            clauses.extend(
                [
                    "created_at >= :start_at",
                    "created_at < :end_exclusive",
                ]
            )
            params["start_at"] = datetime.combine(start, time.min)
            params["end_exclusive"] = datetime.combine(end + timedelta(days=1), time.min)
        if suspected is not None:
            clauses.append("suspected = :suspected")
            params["suspected"] = suspected
        if medication_name:
            clauses.append("LOWER(medication_name) LIKE :medication_name")
            params["medication_name"] = f"%{medication_name.lower()}%"
        if severity:
            clauses.append("severity = :severity")
            params["severity"] = severity
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT id, patient_id, phr_patient_key, medication_name, symptom_text,
                           suspected, severity, matched_effects_json, matched_items_json,
                           evidence, recommendation, source_event_type,
                           related_dose_event_id, created_at
                    FROM ai_v12_side_effect_records
                    WHERE {' AND '.join(clauses)}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        return {
            "success": True,
            "patient_id": patient_id,
            "records": [
                {
                    "id": row["id"],
                    "patient_id": row["patient_id"],
                    "phr_patient_key": row["phr_patient_key"] or "",
                    "medication_name": row["medication_name"],
                    "symptom_text": row["symptom_text"],
                    "suspected": bool(row["suspected"]),
                    "severity": row["severity"],
                    "matched_effects": _json_list(row["matched_effects_json"]),
                    "matched_items": _json_list(row["matched_items_json"]),
                    "evidence": row["evidence"],
                    "recommendation": row["recommendation"],
                    "source_trace_id": "",
                    "source_event_type": row["source_event_type"] or "",
                    "related_dose_event_id": row["related_dose_event_id"],
                    "metadata": {},
                    "created_at": _iso(row["created_at"]),
                }
                for row in rows
            ],
            "total": len(rows),
        }

    def nutrition_preferences(self, *, patient_id: str) -> dict[str, Any]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT predicate, strength, safety_level, confidence,
                           source, evidence_text, node_key, node_type, label
                    FROM ai_v12_nutrition_preferences
                    WHERE patient_id = :patient_id AND status = 'active'
                    ORDER BY safety_level, predicate, label
                    LIMIT :limit
                    """
                ),
                {"patient_id": patient_id, "limit": self.max_rows},
            ).mappings().all()
        facts = [dict(row) for row in rows]
        return {
            "success": True,
            "preferences": {
                "patient_id": patient_id,
                "facts": facts,
                "hard_restrictions": [row for row in facts if row["safety_level"] == "hard"],
                "soft_preferences": [row for row in facts if row["safety_level"] != "hard"],
            },
        }

    def daily_nutrition_summary(self, *, patient_id: str, meal_date: str | None = None) -> dict[str, Any]:
        target = date.fromisoformat(meal_date) if meal_date else datetime.now(UTC).date()
        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT COUNT(DISTINCT m.id) AS total_meals,
                           COALESCE(SUM(f.calories), 0) AS calories,
                           COALESCE(SUM(f.protein), 0) AS protein,
                           COALESCE(SUM(f.sodium), 0) AS sodium,
                           COALESCE(SUM(f.fat), 0) AS fat,
                           COALESCE(SUM(f.carbohydrates), 0) AS carbohydrates
                    FROM ai_v12_nutrition_meals m
                    LEFT JOIN ai_v12_nutrition_foods f ON f.meal_id = m.id
                    WHERE m.patient_id = :patient_id AND m.meal_date = :meal_date
                    """
                ),
                {"patient_id": patient_id, "meal_date": target},
            ).mappings().one()
        return {
            "success": True,
            "daily_summary": {
                "patient_id": patient_id,
                "date": target.isoformat(),
                "total_meals": int(row["total_meals"] or 0),
                "totals": {
                    "calories": float(row["calories"] or 0),
                    "protein": float(row["protein"] or 0),
                    "sodium": float(row["sodium"] or 0),
                    "fat": float(row["fat"] or 0),
                    "carbohydrates": float(row["carbohydrates"] or 0),
                },
                "source": "backend_read_db",
            },
        }

    def recommendation_candidates(self, *, patient_id: str, limit: int = 5) -> dict[str, Any]:
        bounded_limit = self._limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT food_ref_id, food_name, category, serving_size, energy,
                           carbohydrate, protein, fat, sodium, source, manufacturer
                    FROM ai_v12_nutrition_food_ref
                    ORDER BY food_name
                    LIMIT :limit
                    """
                ),
                {"limit": bounded_limit},
            ).mappings().all()
        return {
            "success": True,
            "patient_id": patient_id,
            "recommendations": [_food_ref_view(row) for row in rows],
            "total": len(rows),
            "source": "backend_read_db",
        }

    def record_version(
        self,
        *,
        patient_id: str,
        resource_type: str,
        record_id: str | int,
        parent_record_id: str | int | None = None,
    ) -> int:
        with self.engine.connect() as connection:
            if resource_type == "medication_dose_event":
                resolved = _resolve_external_id(connection, "dose_event", record_id)
                if resolved is None:
                    raise BackendRecordNotFound(f"{resource_type}_not_found")
                version = connection.execute(
                    text(
                        """
                        SELECT version
                        FROM ai_v12_dose_events
                        WHERE id = :record_id AND patient_id = :patient_id
                        """
                    ),
                    {"record_id": resolved[0], "patient_id": patient_id},
                ).scalar_one_or_none()
            elif resource_type == "nutrition_meal":
                resolved = _resolve_external_id(connection, "meal", record_id)
                if resolved is None:
                    raise BackendRecordNotFound(f"{resource_type}_not_found")
                version = connection.execute(
                    text(
                        """
                        SELECT version
                        FROM ai_v12_nutrition_meals
                        WHERE id = :record_id AND patient_id = :patient_id
                        """
                    ),
                    {"record_id": resolved[0], "patient_id": patient_id},
                ).scalar_one_or_none()
            elif resource_type == "nutrition_food":
                resolved = _resolve_external_id(connection, "food", record_id)
                resolved_parent = _resolve_external_id(connection, "meal", parent_record_id)
                if resolved is None or resolved_parent is None:
                    raise BackendRecordNotFound("nutrition_food_not_found")
                version = connection.execute(
                    text(
                        """
                        SELECT f.version
                        FROM ai_v12_nutrition_foods f
                        JOIN ai_v12_nutrition_meals m ON m.id = f.meal_id
                        WHERE f.id = :record_id
                          AND f.meal_id = :parent_record_id
                          AND m.patient_id = :patient_id
                        """
                    ),
                    {
                        "record_id": resolved[0],
                        "parent_record_id": resolved_parent[0],
                        "patient_id": patient_id,
                    },
                ).scalar_one_or_none()
            else:
                raise ValueError(f"unsupported_versioned_resource:{resource_type}")
        if version is None:
            raise BackendRecordNotFound(f"{resource_type}_not_found")
        return max(1, int(version))

    def notification_policies(
        self,
        *,
        patient_id: str,
        policy_id: str | None = None,
        slot_label: str | None = None,
        active_only: bool = True,
    ) -> dict[str, Any]:
        clauses = ["patient_id = :patient_id"]
        params: dict[str, Any] = {
            "patient_id": patient_id,
            "limit": self.max_rows,
        }
        if policy_id:
            clauses.append("public_id = :policy_id")
            params["policy_id"] = policy_id
        if slot_label:
            clauses.append("slot_label = :slot_label")
            params["slot_label"] = slot_label
        if active_only:
            clauses.append("active = :active")
            params["active"] = True
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT public_id, policy_key, slot_label, extra_reminders,
                           interval_minutes, missed_dose_after_minutes,
                           primary_reminder_timing, primary_reminder_offset_minutes,
                           effective_start_date, effective_end_date, active, version
                    FROM ai_v12_reminder_policies
                    WHERE {' AND '.join(clauses)}
                    ORDER BY active DESC, effective_start_date DESC, id DESC
                    LIMIT :limit
                    """
                ),
                params,
            ).mappings().all()
        return {
            "success": True,
            "policies": [
                {
                    "policy_id": row["public_id"],
                    "policy_key": row["policy_key"],
                    "slot_label": row["slot_label"],
                    "extra_reminders": row["extra_reminders"],
                    "interval_minutes": row["interval_minutes"],
                    "missed_dose_after_minutes": row["missed_dose_after_minutes"],
                    "primary_reminder_timing": row["primary_reminder_timing"],
                    "primary_reminder_offset_minutes": row["primary_reminder_offset_minutes"],
                    "effective_start_date": _as_date(row["effective_start_date"]).isoformat(),
                    "effective_end_date": _as_date(row["effective_end_date"]).isoformat(),
                    "active": bool(row["active"]),
                    "version": max(1, int(row["version"] or 1)),
                }
                for row in rows
            ],
            "total": len(rows),
            "contract_bounds": {
                "extra_reminders": {
                    "minimum": POLICY_MIN_EXTRA_REMINDERS,
                    "maximum": POLICY_MAX_EXTRA_REMINDERS,
                },
                "interval_minutes": {
                    "minimum": POLICY_MIN_INTERVAL_MINUTES,
                    "maximum": POLICY_MAX_INTERVAL_MINUTES,
                },
                "missed_dose_after_minutes": {
                    "minimum": POLICY_MIN_MISSED_DOSE_AFTER_MINUTES,
                    "maximum": POLICY_MAX_MISSED_DOSE_AFTER_MINUTES,
                },
                "primary_reminder_timing": ["before", "at", "after"],
                "primary_reminder_offset_minutes": {
                    "minimum": POLICY_MIN_PRIMARY_REMINDER_OFFSET_MINUTES,
                    "maximum": POLICY_MAX_PRIMARY_REMINDER_OFFSET_MINUTES,
                },
                "maximum_effective_days": POLICY_MAX_EFFECTIVE_DAYS,
            },
            "source": "backend_read_db",
        }

    def notification_policy_version(self, *, patient_id: str, policy_id: str) -> int:
        result = self.notification_policies(
            patient_id=patient_id,
            policy_id=policy_id,
            active_only=False,
        )
        policies = result["policies"]
        if len(policies) != 1:
            raise BackendRecordNotFound("notification_policy_not_found")
        return int(policies[0]["version"])

    def _limit(self, value: int) -> int:
        return max(1, min(int(value), self.max_rows))


def _resolve_external_id(
    connection,
    entity_type: str,
    external_id: Any,
) -> tuple[str, str] | None:
    """Resolve public IDs first, with numeric-string lookup only for migration compatibility."""

    value = str(external_id or "").strip()
    if not value:
        return None
    row = connection.execute(
        text(
            """
            SELECT public_id, legacy_id
            FROM ai_v12_legacy_id_map
            WHERE entity_type = :entity_type AND public_id = :external_id
            """
        ),
        {"entity_type": entity_type, "external_id": value},
    ).mappings().first()
    if row is not None:
        return str(row["public_id"]), str(row["legacy_id"])

    legacy_id = _legacy_positive_id(value)
    if legacy_id is None:
        return None
    row = connection.execute(
        text(
            """
            SELECT public_id, legacy_id
            FROM ai_v12_legacy_id_map
            WHERE entity_type = :entity_type AND legacy_id = :legacy_id
            """
        ),
        {"entity_type": entity_type, "legacy_id": str(legacy_id)},
    ).mappings().first()
    if row is None:
        return None
    return str(row["public_id"]), str(row["legacy_id"])


def _legacy_positive_id(value: str) -> int | None:
    if not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _read_only_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )

        @event.listens_for(engine, "connect")
        def _sqlite_read_only(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA query_only = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
            cursor.close()

        return engine

    if not url.drivername.startswith("postgresql"):
        raise ValueError(f"backend_read_database_unsupported_driver:{url.drivername}")

    engine = create_engine(database_url, pool_pre_ping=True, future=True)

    @event.listens_for(engine, "connect")
    def _database_read_only(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
        cursor.close()
        dbapi_connection.commit()

    return engine


def _date_range(
    target_date: str | None,
    start_date: str | None,
    end_date: str | None,
    *,
    max_days: int,
) -> tuple[date, date, date | None]:
    target = date.fromisoformat(target_date) if target_date else None
    if target is not None:
        return target, target, target
    start = date.fromisoformat(start_date) if start_date else datetime.now(UTC).date()
    end = date.fromisoformat(end_date) if end_date else start
    if end < start or (end - start).days + 1 > max_days:
        raise ValueError("invalid_query_date_range")
    return start, end, None


def _food_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "food_ref_id": row["food_ref_id"],
        "food_name": row["food_name"],
        "portion": row["portion"],
        "version": row["version"] or 1,
        "nutrients": {
            "칼로리": {"value": row["calories"], "unit": "kcal"},
            "단백질": {"value": row["protein"], "unit": "g"},
            "나트륨": {"value": row["sodium"], "unit": "mg"},
            "지방": {"value": row["fat"], "unit": "g"},
            "탄수화물": {"value": row["carbohydrates"], "unit": "g"},
        },
    }


def _food_ref_view(row: Any) -> dict[str, Any]:
    return {
        "food_ref_id": row["food_ref_id"],
        "food_name": row["food_name"],
        "category": row["category"] or "",
        "portion": f"{row['serving_size']}g" if row["serving_size"] else "1인분",
        "nutrients": {
            "calories": row["energy"] or 0,
            "carbohydrates": row["carbohydrate"] or 0,
            "protein": row["protein"] or 0,
            "fat": row["fat"] or 0,
            "sodium": row["sodium"] or 0,
        },
        "source": row["source"] or "",
        "manufacturer": row["manufacturer"] or "",
    }


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _json_list(value: Any) -> list[str]:
    import json

    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []
