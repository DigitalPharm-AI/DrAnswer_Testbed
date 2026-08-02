from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from agent_app.tools.backend_food_queries import (
    expanded_food_candidate_rows,
    food_record_view,
    food_reference_view,
    normalize_food_search_text,
)
from shared.backend_read_contract import (
    BACKEND_READ_CONTRACT_VERSION,
    BACKEND_READ_NON_NULL_INVARIANTS,
    BACKEND_READ_VIEW_COLUMNS,
    BACKEND_READ_VIEW_DEFINITIONS,
)
from shared.backend_v13_contracts import (
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
from shared.chat_contracts import ChatSyncRequest
from shared.db import DatabaseEngineConfig, create_database_engine
from shared.public_ids import require_public_id
from shared.schemas import DailyMedicationPattern, DosePatternEvent, SlotAdherenceSummary
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

    def __init__(
        self,
        database_url: str,
        *,
        max_rows: int = 100,
        engine_config: DatabaseEngineConfig | None = None,
    ) -> None:
        if not database_url.strip():
            raise ValueError("backend_read_database_url_required")
        self.engine = _read_only_engine(
            database_url,
            config=engine_config,
        )
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
            engine_config=DatabaseEngineConfig(
                pool_size=resolved.backend_read_db_pool_size,
                max_overflow=resolved.backend_read_db_max_overflow,
                pool_timeout_seconds=(
                    resolved.backend_read_db_pool_timeout_seconds
                ),
                pool_recycle_seconds=(
                    resolved.backend_read_db_pool_recycle_seconds
                ),
                statement_timeout_ms=(
                    resolved.backend_read_db_statement_timeout_ms
                ),
                lock_timeout_ms=resolved.backend_read_db_lock_timeout_ms,
            ),
        )

    def verify_contract(self) -> dict[str, Any]:
        """Verify the live view schema and DB-enforced read-only session."""

        try:
            with self.engine.connect() as connection:
                dialect = connection.dialect.name
                if dialect != "postgresql":
                    raise BackendReadContractError(
                        f"backend_read_contract_postgresql_required:{dialect}"
                    )
                version_info = connection.dialect.server_version_info or ()
                database_version = (
                    ".".join(str(part) for part in version_info)
                    or "unknown"
                )
                value = connection.execute(
                    text("SHOW transaction_read_only")
                ).scalar_one()
                read_only = str(value).strip().lower() in {
                    "on",
                    "true",
                    "1",
                }
                if not read_only:
                    raise BackendReadContractError("backend_read_connection_not_read_only")

                inspector = inspect(connection)
                live_views = set(inspector.get_view_names())
                expected_views = set(BACKEND_READ_VIEW_COLUMNS)
                unexpected_versioned_views = sorted(
                    view_name
                    for view_name in live_views
                    if view_name.startswith("ai_v")
                    and view_name not in expected_views
                )
                if unexpected_versioned_views:
                    raise BackendReadContractError(
                        "backend_read_contract_unexpected_versioned_views:"
                        + ",".join(unexpected_versioned_views)
                    )
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
                if dialect == "postgresql":
                    _verify_postgresql_reader_privileges(connection)
        except BackendReadContractError:
            raise
        except Exception as exc:
            if isinstance(exc, SQLAlchemyTimeoutError):
                raise BackendReadContractError(
                    "backend_read_database_pool_exhausted"
                ) from exc
            raise BackendReadContractError(
                f"backend_read_contract_probe_failed:{type(exc).__name__}"
            ) from exc
        return {
            "ok": True,
            "contract_version": BACKEND_READ_CONTRACT_VERSION,
            "dialect": dialect,
            "server_version": database_version,
            "read_only": True,
            "views": sorted(BACKEND_READ_VIEW_COLUMNS),
        }

    def validate_chat_message(
        self,
        request: ChatSyncRequest,
        *,
        history_limit: int | None = None,
    ) -> dict[str, Any]:
        # Read the complete patient-scoped conversation available inside the
        # configured Backend read cap. The cap is a fail-safe for the model
        # context, not a turn-level truncation hidden from callers.
        limit = self._limit(
            self.max_rows if history_limit is None else history_limit
        )
        with self.engine.connect() as connection:
            message = connection.execute(
                text(
                    """
                    SELECT id, patient_id, role, content,
                           conversation_at, recorded_at,
                           conversation_sequence,
                           message_type, message_payload_json,
                           reply_to_message_id, metadata_json
                    FROM ai_v13_chat_messages
                    WHERE id = :message_id
                      AND patient_id = :patient_id
                      AND role = 'user'
                    """
                ),
                {
                    "message_id": request.message_id,
                    "patient_id": request.patient_id,
                },
            ).mappings().first()
            if message is None or str(message["content"]) != request.message:
                raise BackendChatMessageNotFound("backend_chat_message_not_found")
            rows = connection.execute(
                text(
                    """
                    SELECT c.id, c.role, c.message_type, c.content,
                           c.message_payload_json, c.reply_to_message_id,
                           c.conversation_at,
                           c.conversation_sequence
                    FROM ai_v13_chat_messages c
                    WHERE c.patient_id = :patient_id
                      AND c.conversation_sequence <= :current_sequence
                    ORDER BY c.conversation_sequence DESC
                    LIMIT :limit
                    """
                ),
                {
                    "patient_id": request.patient_id,
                    "current_sequence": message[
                        "conversation_sequence"
                    ],
                    "limit": limit + 1,
                },
            ).mappings().all()
            history_truncated = len(rows) > limit
            rows = rows[:limit]
            rows = _causally_order_chat_rows(rows)
            structured_response_context = _structured_response_context(
                connection,
                current_message=message,
                patient_id=request.patient_id,
                submitted_value=request.message,
                requested_return_type=request.requested_return_type,
            )
        message_metadata = _json_object(message["metadata_json"])
        result = {
            "backend_message_verified": True,
            "missed_dose_reply": (
                message_metadata.get("missed_dose_reply")
                if isinstance(
                    message_metadata.get("missed_dose_reply"),
                    dict,
                )
                else None
            ),
            "recent_chat_complete": not history_truncated,
            "recent_chat_limit": limit,
            "recent_chat": [
                {
                    "message_id": str(row["id"]),
                    "role": str(row["role"]),
                    "message_type": str(row["message_type"] or "text"),
                    "content": str(row["content"]),
                    "message": _json_object(row["message_payload_json"]),
                    "reply_to_message_id": (
                        str(row["reply_to_message_id"])
                        if row["reply_to_message_id"] is not None
                        else None
                    ),
                    "conversation_at": _iso(
                        row["conversation_at"]
                    ),
                }
                for row in rows
            ],
        }
        if structured_response_context is not None:
            result["structured_response_context"] = (
                structured_response_context
            )
        return result

    def validate_feedback_target(
        self,
        *,
        message_id: str,
        patient_id: str,
    ) -> dict[str, Any]:
        """Resolve feedback only to an assistant message in the trusted chat scope."""

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, patient_id, role
                    FROM ai_v13_chat_messages
                    WHERE id = :message_id
                      AND patient_id = :patient_id
                      AND role = 'assistant'
                    """
                ),
                {
                    "message_id": message_id,
                    "patient_id": patient_id,
                },
            ).mappings().first()
        if row is None:
            raise BackendChatMessageNotFound("backend_chat_message_not_found")
        return {
            "message_id": str(row["id"]),
            "patient_id": str(row["patient_id"]),
            "role": str(row["role"]),
        }

    def patient_context_snapshot(
        self,
        *,
        patient_id: str,
        as_of: datetime,
        _connection: Connection | None = None,
    ) -> dict[str, Any]:
        """Build the trusted, current patient context loaded before the LLM.

        A domain-level query failure is represented explicitly so the LLM can
        continue in a constrained mode. Message/patient scope verification is
        deliberately performed separately and remains fail-closed.
        """

        if _connection is None:
            with self.engine.connect() as connection:
                with connection.begin():
                    connection.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"
                    )
                    return self.patient_context_snapshot(
                        patient_id=patient_id,
                        as_of=as_of,
                        _connection=connection,
                    )

        target_date = as_of.date()
        availability: dict[str, str] = {
            "profile": "unavailable",
            "conditions_and_treatments": "unavailable",
            "today_medication": "unavailable",
            "today_meals": "unavailable",
            "notification_policies": "unavailable",
            "allergies": "not_supported",
            "clinical_observations": "not_supported",
        }
        profile: dict[str, Any] | None = None
        schedules: list[dict[str, Any]] = []
        dose_status: dict[str, Any] = {
            "dose_events": [],
            "total": 0,
            "totals_by_status": {},
        }
        meals: list[dict[str, Any]] = []
        policies: list[dict[str, Any]] = []
        profile_query_ok = False
        schedule_query_ok = False
        dose_query_ok = False

        try:
            with _connection.begin_nested():
                profile = self._patient_profile(
                    patient_id=patient_id,
                    _connection=_connection,
                )
            profile_query_ok = True
            availability["profile"] = "available" if profile is not None else "not_found"
        except Exception:
            profile = None

        try:
            with _connection.begin_nested():
                schedules = self._active_medication_schedules(
                    patient_id=patient_id,
                    target_date=target_date,
                    _connection=_connection,
                )
            schedule_query_ok = True
        except Exception:
            schedules = []
        try:
            with _connection.begin_nested():
                dose_status = self.medication_dose_status(
                    patient_id=patient_id,
                    target_date=target_date.isoformat(),
                    _connection=_connection,
                )
            dose_query_ok = True
        except Exception:
            dose_status = {
                "dose_events": [],
                "total": 0,
                "totals_by_status": {},
            }
        if not (schedule_query_ok and dose_query_ok):
            availability["today_medication"] = "unavailable"
        elif schedules or dose_status.get("total"):
            availability["today_medication"] = "available"
        else:
            availability["today_medication"] = "not_found"

        try:
            with _connection.begin_nested():
                meal_result = self.nutrition_meals(
                    patient_id=patient_id,
                    meal_date=target_date.isoformat(),
                    _connection=_connection,
                )
            meals = list(meal_result.get("meals") or [])
            availability["today_meals"] = "available" if meals else "not_found"
        except Exception:
            meals = []

        try:
            with _connection.begin_nested():
                policies = self._active_notification_policies(
                    patient_id=patient_id,
                    target_date=target_date,
                    _connection=_connection,
                )
            availability["notification_policies"] = (
                "available" if policies else "not_found"
            )
        except Exception:
            policies = []

        conditions = []
        if profile and str(profile.get("disease") or "").strip():
            conditions.append(str(profile["disease"]).strip())
        treatments = list(
            dict.fromkeys(
                str(item.get("treatment_area") or "").strip()
                for item in schedules
                if str(item.get("treatment_area") or "").strip()
            )
        )
        if not (profile_query_ok and schedule_query_ok):
            availability["conditions_and_treatments"] = "unavailable"
        else:
            availability["conditions_and_treatments"] = (
                "available" if conditions or treatments else "not_found"
            )

        core_statuses = (
            availability["profile"],
            availability["conditions_and_treatments"],
            availability["today_medication"],
            availability["today_meals"],
            availability["notification_policies"],
        )
        return {
            "patient_id": patient_id,
            "as_of": _iso(as_of),
            "date": target_date.isoformat(),
            "read_contract_version": BACKEND_READ_CONTRACT_VERSION,
            "context_mode": (
                "partial"
                if any(status in {"unavailable", "not_supported"} for status in core_statuses)
                else "complete"
            ),
            "availability": availability,
            "profile": profile,
            "active_conditions": conditions,
            "active_treatments": treatments,
            "active_medication_schedules": schedules,
            "today_medication": {
                "schedules": schedules,
                "dose_events": list(dose_status.get("dose_events") or []),
                "total": int(dose_status.get("total") or 0),
                "totals_by_status": dict(dose_status.get("totals_by_status") or {}),
            },
            "today_meals": meals,
            "active_notification_policies": policies,
        }

    def daily_pattern_context(
        self,
        *,
        patient_id: str,
        analysis_date: date,
        window_days: int = 7,
    ) -> dict[str, Any]:
        """Build the Agent-internal daily pattern from read-only Backend data."""

        require_public_id(patient_id, "patient")
        bounded_window_days = max(1, min(int(window_days), 31))
        window_start = analysis_date - timedelta(
            days=bounded_window_days - 1,
        )
        schedules = self._active_medication_schedules(
            patient_id=patient_id,
            target_date=analysis_date,
        )
        dose_result = self.medication_dose_status(
            patient_id=patient_id,
            start_date=window_start.isoformat(),
            end_date=analysis_date.isoformat(),
        )
        raw_events = list(dose_result.get("dose_events") or [])
        events: list[DosePatternEvent] = []
        slot_counts: dict[str, Counter[str]] = defaultdict(Counter)
        observed_dates: set[date] = set()
        for raw_event in raw_events:
            status = str(raw_event.get("status") or "")
            if status not in {"scheduled", "taken", "missed"}:
                raise BackendReadContractError(
                    "backend_daily_pattern_status_invalid"
                )
            scheduled_for = datetime.fromisoformat(
                str(raw_event["scheduled_for"]),
            )
            taken_at_raw = raw_event.get("taken_at")
            events.append(
                DosePatternEvent(
                    dose_event_id=str(raw_event["dose_event_id"]),
                    medication_name=str(
                        raw_event.get("medication_name") or "",
                    ),
                    slot_label=str(raw_event.get("slot_label") or ""),
                    scheduled_for=scheduled_for,
                    taken_at=(
                        datetime.fromisoformat(str(taken_at_raw))
                        if taken_at_raw
                        else None
                    ),
                    status=status,
                )
            )
            observed_dates.add(scheduled_for.date())
            slot_counts[str(raw_event.get("slot_label") or "")][
                status
            ] += 1

        schedule_slots = sorted(
            {
                str(schedule.get("slot_label") or "")
                for schedule in schedules
                if str(schedule.get("slot_label") or "")
            }
            | {
                event.slot_label
                for event in events
                if event.slot_label
            }
        )
        summaries: list[SlotAdherenceSummary] = []
        for slot_label in schedule_slots:
            counts = slot_counts[slot_label]
            scheduled_count = sum(counts.values())
            missed_count = int(counts.get("missed", 0))
            summaries.append(
                SlotAdherenceSummary(
                    slot_label=slot_label,
                    scheduled_count=scheduled_count,
                    taken_count=int(counts.get("taken", 0)),
                    missed_count=missed_count,
                    miss_rate=(
                        missed_count / scheduled_count
                        if scheduled_count
                        else 0.0
                    ),
                )
            )
        current_policies = self._active_notification_policies(
            patient_id=patient_id,
            target_date=analysis_date,
        )
        pattern = DailyMedicationPattern(
            patient_id=patient_id,
            date=analysis_date,
            window_start_date=window_start,
            window_end_date=analysis_date,
            window_days=bounded_window_days,
            observed_day_count=len(observed_dates),
            schedule_slots=schedule_slots,
            dose_events=events,
            slot_summaries=summaries,
            active_schedule_snapshot=schedules,
            current_policy_snapshot=current_policies,
            notes="agent_internal_read_only_backend_pattern",
        )
        return pattern.model_dump(mode="json")

    def _patient_profile(
        self,
        *,
        patient_id: str,
        _connection: Connection | None = None,
    ) -> dict[str, Any] | None:
        if _connection is None:
            with self.engine.connect() as connection:
                return self._patient_profile(
                    patient_id=patient_id,
                    _connection=connection,
                )
        row = _connection.execute(
            text(
                """
                SELECT patient_id, age, gender, height, weight, disease,
                       activity_level, egfr, ckd_stage, ckd_risk, updated_at
                FROM ai_v13_patient_profiles
                WHERE patient_id = :patient_id
                LIMIT 1
                """
            ),
            {"patient_id": patient_id},
        ).mappings().first()
        if row is None:
            return None
        return {
            "patient_id": str(row["patient_id"]),
            "age": row["age"],
            "gender": row["gender"],
            "height": row["height"],
            "weight": row["weight"],
            "disease": row["disease"] or "",
            "activity_level": row["activity_level"] or "",
            "egfr": row["egfr"],
            "ckd_stage": row["ckd_stage"] or "",
            "ckd_risk": row["ckd_risk"] or "",
            "updated_at": _iso(row["updated_at"]) if row["updated_at"] else None,
        }

    def _active_medication_schedules(
        self,
        *,
        patient_id: str,
        target_date: date,
        _connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        if _connection is None:
            with self.engine.connect() as connection:
                return self._active_medication_schedules(
                    patient_id=patient_id,
                    target_date=target_date,
                    _connection=connection,
                )
        rows = _connection.execute(
            text(
                """
                SELECT patient_id, medication_name, dosage, instructions,
                       treatment_area, start_date, end_date, slot_label,
                       scheduled_time, source_type, source_key
                FROM ai_v13_active_medication_schedules
                WHERE patient_id = :patient_id
                  AND start_date <= :target_date
                  AND end_date >= :target_date
                ORDER BY scheduled_time, medication_name, slot_label
                LIMIT :limit
                """
            ),
            {
                "patient_id": patient_id,
                "target_date": target_date,
                "limit": self.max_rows,
            },
        ).mappings().all()
        return [
            {
                "medication_name": row["medication_name"],
                "dosage": row["dosage"] or "",
                "instructions": row["instructions"] or "",
                "treatment_area": row["treatment_area"] or "",
                "start_date": _as_date(row["start_date"]).isoformat(),
                "end_date": _as_date(row["end_date"]).isoformat(),
                "slot_label": row["slot_label"],
                "scheduled_time": row["scheduled_time"],
                "source_type": row["source_type"] or "",
                "source_key": row["source_key"] or "",
            }
            for row in rows
        ]

    def _active_notification_policies(
        self,
        *,
        patient_id: str,
        target_date: date,
        _connection: Connection | None = None,
    ) -> list[dict[str, Any]]:
        if _connection is None:
            with self.engine.connect() as connection:
                return self._active_notification_policies(
                    patient_id=patient_id,
                    target_date=target_date,
                    _connection=connection,
                )
        rows = _connection.execute(
            text(
                """
                SELECT id, policy_key, slot_label, extra_reminders,
                       interval_minutes, missed_dose_after_minutes,
                       primary_reminder_timing,
                       primary_reminder_offset_minutes,
                       effective_start_date, effective_end_date, version
                FROM ai_v13_reminder_policies
                WHERE patient_id = :patient_id
                  AND active = TRUE
                  AND effective_start_date <= :target_date
                  AND effective_end_date >= :target_date
                ORDER BY slot_label, policy_key, id
                LIMIT :limit
                """
            ),
            {
                "patient_id": patient_id,
                "target_date": target_date,
                "limit": self.max_rows,
            },
        ).mappings().all()
        return [
            {
                "policy_id": str(row["id"]),
                "policy_key": row["policy_key"],
                "slot_label": row["slot_label"],
                "extra_reminders": row["extra_reminders"],
                "interval_minutes": row["interval_minutes"],
                "missed_dose_after_minutes": row["missed_dose_after_minutes"],
                "primary_reminder_timing": row["primary_reminder_timing"],
                "primary_reminder_offset_minutes": row[
                    "primary_reminder_offset_minutes"
                ],
                "effective_start_date": _as_date(
                    row["effective_start_date"]
                ).isoformat(),
                "effective_end_date": _as_date(row["effective_end_date"]).isoformat(),
                "version": row["version"],
            }
            for row in rows
        ]

    def medication_dose_status(
        self,
        *,
        patient_id: str,
        target_date: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | None = None,
        medication_name: str | None = None,
        _connection: Connection | None = None,
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
        if _connection is None:
            with self.engine.connect() as connection:
                return self.medication_dose_status(
                    patient_id=patient_id,
                    target_date=target_date,
                    start_date=start_date,
                    end_date=end_date,
                    status=status,
                    medication_name=medication_name,
                    _connection=connection,
                )
        rows = _connection.execute(
            text(
                f"""
                SELECT id, patient_id, medication_name, slot_label, scheduled_for,
                       status, taken_at, note, version
                FROM ai_v13_dose_events
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

    def missed_dose_event_context(
        self,
        *,
        patient_id: str,
        dose_event_id: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve one public dose ID inside the asserted patient scope.

        The query deliberately combines both identifiers in SQL. A caller can
        therefore never learn or process another patient's event by guessing a
        valid public ID.
        """

        with self.engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT id, patient_id, medication_name, slot_label,
                           scheduled_for, status, taken_at, note, version,
                           missed_dose_request_id,
                           adherence_pattern_context_json,
                           tone_policy_context_json
                    FROM ai_v13_dose_events
                    WHERE id = :dose_event_id
                      AND patient_id = :patient_id
                      AND (
                          CAST(:request_id AS TEXT) IS NULL
                          OR missed_dose_request_id = :request_id
                      )
                    LIMIT 1
                    """
                ),
                {
                    "dose_event_id": dose_event_id,
                    "patient_id": patient_id,
                    "request_id": request_id,
                },
            ).mappings().first()
        if row is None:
            if request_id:
                raise BackendRecordNotFound(
                    "backend_missed_dose_feedback_context_not_found_or_scope_mismatch"
                )
            raise BackendRecordNotFound(
                "backend_dose_event_not_found_or_scope_mismatch"
            )

        patient_snapshot = self.patient_context_snapshot(
            patient_id=patient_id,
            as_of=datetime.now(ZoneInfo("Asia/Seoul")),
        )
        if str(patient_snapshot.get("patient_id") or "") != patient_id:
            raise BackendReadContractError(
                "backend_patient_context_scope_mismatch"
            )
        return {
            "dose_event_id": str(row["id"]),
            "patient_id": str(row["patient_id"]),
            "medication_name": str(row["medication_name"] or ""),
            "slot_label": str(row["slot_label"] or ""),
            "scheduled_for": row["scheduled_for"],
            "status": str(row["status"] or ""),
            "taken_at": row["taken_at"],
            "note": str(row["note"] or ""),
            "version": int(row["version"] or 1),
            "patient_snapshot": patient_snapshot,
            "adherence_pattern_context": _json_object(
                row["adherence_pattern_context_json"]
            ),
            "tone_policy_context": _json_object(
                row["tone_policy_context_json"]
            ),
        }

    def nutrition_meals(
        self,
        *,
        patient_id: str,
        meal_date: str | None = None,
        _connection: Connection | None = None,
    ) -> dict[str, Any]:
        clauses = ["m.patient_id = :patient_id"]
        params: dict[str, Any] = {"patient_id": patient_id, "limit": self.max_rows}
        if meal_date:
            clauses.append("m.meal_date = :meal_date")
            params["meal_date"] = date.fromisoformat(meal_date)
        if _connection is None:
            with self.engine.connect() as connection:
                return self.nutrition_meals(
                    patient_id=patient_id,
                    meal_date=meal_date,
                    _connection=connection,
                )
        meals = _connection.execute(
            text(
                f"""
                SELECT m.id, m.patient_id, m.meal_type, m.meal_date, m.meal_time,
                       m.scenario_key, m.description, m.version
                FROM ai_v13_nutrition_meals m
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
            placeholders = ", ".join(
                f":meal_{index}" for index in range(len(meal_ids))
            )
            food_params = {
                f"meal_{index}": meal_id
                for index, meal_id in enumerate(meal_ids)
            }
            foods = _connection.execute(
                text(
                    f"""
                    SELECT id, meal_id, food_ref_id, food_name, portion, calories,
                           protein, sodium, fat, carbohydrates, version
                    FROM ai_v13_nutrition_foods
                    WHERE meal_id IN ({placeholders})
                    ORDER BY meal_id, id
                    """
                ),
                food_params,
            ).mappings().all()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in foods:
            grouped[str(row["meal_id"])].append(food_record_view(row))
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
        normalized = normalize_food_search_text(query)
        if not normalized:
            return {"success": False, "error": "query_required", "candidates": [], "source": "db"}
        bounded_limit = self._limit(limit)
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    """
                    SELECT food_ref_id, food_name, category, serving_size, energy,
                           carbohydrate, protein, fat, sodium, source, manufacturer
                    FROM ai_v13_nutrition_food_ref
                    WHERE LOWER(food_name) LIKE :query
                    ORDER BY CASE
                                 WHEN LOWER(food_name) = :exact THEN 0
                                 WHEN LOWER(food_name) LIKE :prefix THEN 1
                                 ELSE 2
                             END,
                             LENGTH(food_name),
                             LOWER(food_name),
                             food_ref_id
                    LIMIT :limit
                    """
                ),
                {
                    "query": f"%{query.strip().lower()}%",
                    "prefix": f"{query.strip().lower()}%",
                    "exact": query.strip().lower(),
                    "limit": bounded_limit,
                },
            ).mappings().all()
            match_mode = "direct"
            if not rows:
                rows = expanded_food_candidate_rows(
                    connection,
                    normalized_query=normalized,
                    limit=bounded_limit,
                    max_rows=self.max_rows,
                )
                match_mode = "expanded" if rows else "none"
        return {
            "success": True,
            "candidates": [food_reference_view(row) for row in rows],
            "source": "backend_read_db",
            "error": "",
            "query": query,
            "meal_type": "",
            "limit": bounded_limit,
            "match_mode": match_mode,
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
        with self.engine.connect() as connection:
            rows = connection.execute(
                text(
                    f"""
                    SELECT id, patient_id, medication_name, symptom_text,
                           symptom_onset_text, suspected,
                           severity_result_json, matched_effects_json,
                           matched_items_json, related_dose_event_id,
                           version, created_at, updated_at
                    FROM ai_v13_side_effect_records
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
                    "medication_name": row["medication_name"],
                    "symptom_text": row["symptom_text"],
                    "symptom_onset_text": row["symptom_onset_text"],
                    "suspected": bool(row["suspected"]),
                    "severity": _json_object(
                        row["severity_result_json"]
                    ),
                    "matched_effects": _json_list(row["matched_effects_json"]),
                    "matched_items": _json_list(row["matched_items_json"]),
                    "related_dose_event_id": row["related_dose_event_id"],
                    "version": row["version"],
                    "created_at": _iso(row["created_at"]),
                    "updated_at": _iso(row["updated_at"]),
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
                    FROM ai_v13_nutrition_preferences
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
                    FROM ai_v13_nutrition_meals m
                    LEFT JOIN ai_v13_nutrition_foods f ON f.meal_id = m.id
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
                    FROM ai_v13_nutrition_food_ref
                    ORDER BY food_name
                    LIMIT :limit
                    """
                ),
                {"limit": bounded_limit},
            ).mappings().all()
        return {
            "success": True,
            "patient_id": patient_id,
            "recommendations": [food_reference_view(row) for row in rows],
            "total": len(rows),
            "source": "backend_read_db",
        }

    def record_version(
        self,
        *,
        patient_id: str,
        resource_type: str,
        record_id: str,
        parent_record_id: str | None = None,
    ) -> int:
        with self.engine.connect() as connection:
            if resource_type == "medication_dose_event":
                version = connection.execute(
                    text(
                        """
                        SELECT version
                        FROM ai_v13_dose_events
                        WHERE id = :record_id AND patient_id = :patient_id
                        """
                    ),
                    {"record_id": record_id, "patient_id": patient_id},
                ).scalar_one_or_none()
            elif resource_type == "nutrition_meal":
                version = connection.execute(
                    text(
                        """
                        SELECT version
                        FROM ai_v13_nutrition_meals
                        WHERE id = :record_id AND patient_id = :patient_id
                        """
                    ),
                    {"record_id": record_id, "patient_id": patient_id},
                ).scalar_one_or_none()
            elif resource_type == "nutrition_food":
                if not parent_record_id:
                    raise BackendRecordNotFound("nutrition_food_not_found")
                version = connection.execute(
                    text(
                        """
                        SELECT f.version
                        FROM ai_v13_nutrition_foods f
                        JOIN ai_v13_nutrition_meals m ON m.id = f.meal_id
                        WHERE f.id = :record_id
                          AND f.meal_id = :parent_record_id
                          AND m.patient_id = :patient_id
                        """
                    ),
                    {
                        "record_id": record_id,
                        "parent_record_id": parent_record_id,
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
            clauses.append("id = :policy_id")
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
                    SELECT id, policy_key, slot_label, extra_reminders,
                           interval_minutes, missed_dose_after_minutes,
                           primary_reminder_timing, primary_reminder_offset_minutes,
                           effective_start_date, effective_end_date, active, version
                    FROM ai_v13_reminder_policies
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
                    "policy_id": row["id"],
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


def _structured_response_context(
    connection,
    *,
    current_message: Any,
    patient_id: str,
    submitted_value: str,
    requested_return_type: str,
) -> dict[str, Any] | None:
    """Recover structured-card semantics from the authoritative chat graph.

    The AI chat contract intentionally does not carry a source_message_id.
    Backend already persisted the reply edge, so AI Server resolves that edge
    through its read-only DB projection instead of trusting a model- or
    client-supplied identifier.
    """

    source_message_id = str(
        current_message.get("reply_to_message_id") or ""
    ).strip()
    if not source_message_id:
        return None
    source = connection.execute(
        text(
            """
            SELECT id, role, message_type, content, message_payload_json,
                   reply_to_message_id, conversation_at,
                   conversation_sequence
            FROM ai_v13_chat_messages
            WHERE id = :source_message_id
              AND patient_id = :patient_id
              AND role = 'assistant'
            """
        ),
        {
            "source_message_id": source_message_id,
            "patient_id": patient_id,
        },
    ).mappings().first()
    if source is None:
        return None
    source_type = str(source["message_type"] or "text")
    if source_type not in {"selection_box", "input_box"}:
        return None

    original = None
    original_message_id = str(
        source["reply_to_message_id"] or ""
    ).strip()
    if original_message_id:
        original = connection.execute(
            text(
                """
                SELECT role, message_type, content, message_payload_json,
                       conversation_at, conversation_sequence
                FROM ai_v13_chat_messages
                WHERE id = :original_message_id
                  AND patient_id = :patient_id
                  AND role = 'user'
                """
            ),
            {
                "original_message_id": original_message_id,
                "patient_id": patient_id,
            },
        ).mappings().first()

    result: dict[str, Any] = {
        "kind": "structured_chat_response",
        "response_type": requested_return_type,
        "response_value": submitted_value,
        # Internal deterministic state machines use these Backend-owned
        # reply-edge identifiers. The LLM projection removes *_id fields.
        "source_message_id": str(source["id"]),
        "originating_user_message_id": original_message_id or None,
        "source_message": {
            "message_type": source_type,
            "content": str(source["content"] or ""),
            "message": _json_object(source["message_payload_json"]),
            "conversation_at": _iso(source["conversation_at"]),
        },
    }
    if original is not None:
        result["originating_user_message"] = {
            "message_id": original_message_id,
            "message_type": str(original["message_type"] or "text"),
            "content": str(original["content"] or ""),
            "message": _json_object(original["message_payload_json"]),
            "conversation_at": _iso(original["conversation_at"]),
        }
    return result


def _read_only_engine(
    database_url: str,
    *,
    config: DatabaseEngineConfig | None = None,
) -> Engine:
    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError(
            f"backend_read_database_postgresql_required:{url.drivername}"
        )
    return create_database_engine(
        database_url,
        config=config,
        read_only=True,
    )


def _verify_postgresql_reader_privileges(connection) -> None:
    if bool(
        connection.execute(
            text(
                "SELECT has_database_privilege("
                "current_user, current_database(), 'CREATE')"
            )
        ).scalar_one()
    ):
        raise BackendReadContractError(
            "backend_read_role_database_create_allowed"
        )
    if bool(
        connection.execute(
            text(
                "SELECT has_schema_privilege("
                "current_user, current_schema(), 'CREATE')"
            )
        ).scalar_one()
    ):
        raise BackendReadContractError(
            "backend_read_role_schema_create_allowed"
        )

    granted_select_objects = {
        str(value)
        for value in connection.execute(
            text(
                """
                SELECT relation.relname
                FROM pg_class relation
                JOIN pg_namespace namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = current_schema()
                  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
                  AND has_table_privilege(
                      current_user,
                      relation.oid,
                      'SELECT'
                  )
                ORDER BY relation.relname
                """
            )
        ).scalars()
    }
    unexpected_select_objects = sorted(
        granted_select_objects - set(BACKEND_READ_VIEW_COLUMNS)
    )
    if unexpected_select_objects:
        raise BackendReadContractError(
            "backend_read_unexpected_select_privilege:"
            + ",".join(unexpected_select_objects)
        )

    for view_name in BACKEND_READ_VIEW_COLUMNS:
        if not bool(
            connection.execute(
                text(
                    "SELECT has_table_privilege("
                    "current_user, :object_name, 'SELECT')"
                ),
                {"object_name": view_name},
            ).scalar_one()
        ):
            raise BackendReadContractError(
                f"backend_read_view_select_denied:{view_name}"
            )
        for privilege in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if bool(
                connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        "current_user, :object_name, :privilege)"
                    ),
                    {
                        "object_name": view_name,
                        "privilege": privilege,
                    },
                ).scalar_one()
            ):
                raise BackendReadContractError(
                    f"backend_read_view_write_allowed:{view_name}"
                )

    source_tables = {
        str(table_name)
        for definition in BACKEND_READ_VIEW_DEFINITIONS.values()
        for table_name in dict(definition["sources"])
    }
    for table_name in source_tables:
        if bool(
            connection.execute(
                text(
                    "SELECT has_table_privilege("
                    "current_user, :object_name, 'SELECT')"
                ),
                {"object_name": table_name},
            ).scalar_one()
        ):
            raise BackendReadContractError(
                f"backend_read_source_table_select_allowed:{table_name}"
            )


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


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)


def _causally_order_chat_rows(
    rows: list[Any],
) -> list[Any]:
    """Order messages by Backend sequence while preserving reply causality.

    Public IDs remain opaque resource identifiers. ``conversation_sequence``
    is an internal, ordering-only projection and never replaces those IDs.
    Reply edges provide the authoritative parent-before-child constraint.
    """

    by_id = {str(row["id"]): row for row in rows}
    indegree = {message_id: 0 for message_id in by_id}
    children: dict[str, list[str]] = defaultdict(list)
    for message_id, row in by_id.items():
        parent_value = row["reply_to_message_id"]
        parent_id = (
            str(parent_value)
            if parent_value is not None
            else ""
        )
        if parent_id not in by_id or parent_id == message_id:
            continue
        children[parent_id].append(message_id)
        indegree[message_id] += 1

    def order_key(message_id: str) -> tuple[int, int, str]:
        row = by_id[message_id]
        role = str(row["role"])
        try:
            sequence = int(row["conversation_sequence"])
        except (KeyError, TypeError, ValueError):
            sequence = 0
        return (
            sequence,
            {
                "assistant": 0,
                "system": 1,
                "user": 2,
            }.get(role, 1),
            message_id,
        )

    ready = [
        message_id
        for message_id, degree in indegree.items()
        if degree == 0
    ]
    ordered_ids: list[str] = []
    while ready:
        ready.sort(key=order_key)
        message_id = ready.pop(0)
        ordered_ids.append(message_id)
        for child_id in children.get(message_id, ()):
            indegree[child_id] -= 1
            if indegree[child_id] == 0:
                ready.append(child_id)

    if len(ordered_ids) != len(by_id):
        # Corrupt reply cycles must not make Snapshot assembly unavailable.
        ordered = set(ordered_ids)
        ordered_ids.extend(
            sorted(
                (
                    message_id
                    for message_id in by_id
                    if message_id not in ordered
                ),
                key=order_key,
            )
        )
    return [by_id[message_id] for message_id in ordered_ids]


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


def _json_object(value: Any) -> dict[str, Any]:
    import json

    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
