from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.orm import Session

from shared.chat_contracts import ChatMessageContent
from shared.json_utils import parse_json_object
from shared.settings import get_settings
from system_app.models import ChatMessage, DoseEvent, MedicationPlan, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.nutrition_service import nutrition_dashboard_view
from system_app.services.patient_profile_service import simulation_readiness
from system_app.services.ui_medication_scenario_service import (
    active_test_medication_scenario,
    medication_events_for_date,
)
from system_app.services.ui_feedback_service import (
    feedback_status_from_metadata,
)
from system_app.services.ui_policy_service import ui_policy_state
from system_app.services.ui_time import (
    as_seoul_datetime,
    as_seoul_iso,
    naive_utc_as_seoul,
)

HIDDEN_DELIVERY_CHANNELS = {"chat_only", "internal_only"}
_SEOUL = ZoneInfo("Asia/Seoul")
_CHAT_HISTORY_QUERY_BATCH_SIZE = 128
_CHAT_HISTORY_MESSAGES_PER_TURN = 4
_NUTRITION_UI_INTERNAL_FIELDS = {
    "id",
    "patient_id",
    "version",
    "source_trace_id",
}


class NotificationCursorNotFound(ValueError):
    pass


def is_patient_visible_notification_metadata(metadata: dict[str, Any]) -> bool:
    return metadata.get("delivery_channel") not in HIDDEN_DELIVERY_CHANNELS


def clock_view(session: Session) -> dict[str, Any]:
    clock = ensure_clock(session)
    return {
        "current_time": as_seoul_iso(clock.current_time),
        "is_running": bool(clock.is_running),
        "speed_multiplier": int(clock.speed_multiplier),
    }


def dashboard_view(
    session: Session,
    *,
    target_date: date | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    clock = ensure_clock(session)
    actual_date = target_date or clock.current_time.date()
    readiness = simulation_readiness(session)
    return {
        "clock": clock_view(session),
        "simulation_ready": bool(readiness["ready"]),
        "active_scenario": active_test_medication_scenario(
            session,
            actual_date,
            patient_id=settings.patient_id,
        ),
        "medications": medication_events_for_date(
            session,
            actual_date,
            patient_id=settings.patient_id,
        ),
        "nutrition": nutrition_dashboard_ui_view(
            session,
            patient_id=settings.patient_id,
            target_date=actual_date,
        ),
        "policies": ui_policy_state(session, patient_id=settings.patient_id),
        "notifications": notification_list(
            session,
            after_id=None,
            limit=50,
        )["notifications"],
    }


def nutrition_dashboard_ui_view(
    session: Session,
    *,
    patient_id: str,
    target_date: date | None = None,
) -> dict[str, Any]:
    """Project the internal nutrition view onto the patient-facing UI boundary."""

    internal_view = nutrition_dashboard_view(
        session,
        patient_id=patient_id,
        target_date=target_date,
    )
    projected = _project_nutrition_ui_value(internal_view)
    if not isinstance(projected, dict):
        raise TypeError("nutrition_dashboard_projection_must_be_object")
    return projected


def _project_nutrition_ui_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _project_nutrition_ui_value(item)
            for key, item in value.items()
            if key not in _NUTRITION_UI_INTERNAL_FIELDS
            and not (key.endswith("_id") and isinstance(item, int))
        }
    if isinstance(value, list):
        return [_project_nutrition_ui_value(item) for item in value]
    return value


def dose_view_by_public_id(
    session: Session,
    public_id: str,
) -> dict[str, Any] | None:
    settings = get_settings()
    row = session.execute(
        select(DoseEvent, MedicationPlan.treatment_area)
        .join(MedicationPlan, MedicationPlan.id == DoseEvent.plan_id)
        .where(
            DoseEvent.public_id == public_id,
            DoseEvent.patient_id == settings.patient_id,
        )
    ).first()
    if row is None:
        return None
    event, treatment_area = row
    return {
        "dose_event_id": event.public_id,
        "medication_name": event.medication_name,
        "treatment_area": treatment_area or "",
        "slot_label": event.slot_label,
        "scheduled_for": as_seoul_iso(event.scheduled_for),
        "status": event.status,
        "taken_at": as_seoul_iso(event.taken_at) if event.taken_at else None,
    }


def notification_list(
    session: Session,
    *,
    after_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    settings = get_settings()
    clock = ensure_clock(session)
    cursor: Notification | None = None
    if after_id is not None:
        cursor = session.scalar(
            select(Notification).where(
                Notification.public_id == after_id,
                Notification.patient_id == settings.patient_id,
            )
        )
        if cursor is None:
            raise NotificationCursorNotFound(after_id)

    filters = (
        Notification.patient_id == settings.patient_id,
        Notification.visible_at <= clock.current_time,
        Notification.acknowledged.is_(False),
        Notification.notification_type != "system_policy_request",
    )
    batch_size = max(64, min(limit * 4, 400))
    visible: list[dict[str, Any]] = []
    last_seen_id = after_id

    if cursor is None:
        # A cursor-less request is the latest snapshot. The first candidate is
        # also the high-water mark, including an internal-only notification.
        # Older rows beyond ``limit`` intentionally remain outside the
        # subsequent incremental stream.
        scan_before: Notification | None = None
        while len(visible) < limit:
            statement = select(Notification).where(*filters)
            if scan_before is not None:
                statement = statement.where(
                    or_(
                        Notification.visible_at < scan_before.visible_at,
                        and_(
                            Notification.visible_at
                            == scan_before.visible_at,
                            Notification.id < scan_before.id,
                        ),
                    )
                )
            rows = list(
                session.scalars(
                    statement.order_by(
                        desc(Notification.visible_at),
                        desc(Notification.id),
                    ).limit(batch_size)
                ).all()
            )
            if not rows:
                break
            if last_seen_id is None:
                last_seen_id = rows[0].public_id
            for row in rows:
                metadata = parse_json_object(row.metadata_json)
                if not is_patient_visible_notification_metadata(metadata):
                    continue
                visible.append(_notification_view(session, row, metadata))
                if len(visible) >= limit:
                    break
            if len(visible) >= limit or len(rows) < batch_size:
                break
            scan_before = rows[-1]
    else:
        # Drain new notifications from the cursor forward in chronological
        # batches. Returning each batch newest-first preserves the UI ordering,
        # while advancing only through rows actually inspected prevents a
        # limit or metadata filter from skipping unseen rows. ``visible_at`` is
        # part of the boundary so a previously future-visible row remains
        # eligible after the simulation clock reaches it, even when its
        # internal id is lower than the current cursor's.
        scan_after = cursor
        while len(visible) < limit:
            rows = list(
                session.scalars(
                    select(Notification)
                    .where(
                        *filters,
                        or_(
                            Notification.visible_at
                            > scan_after.visible_at,
                            and_(
                                Notification.visible_at
                                == scan_after.visible_at,
                                Notification.id > scan_after.id,
                            ),
                        ),
                    )
                    .order_by(
                        Notification.visible_at,
                        Notification.id,
                    )
                    .limit(batch_size)
                ).all()
            )
            if not rows:
                break
            for row in rows:
                last_seen_id = row.public_id
                scan_after = row
                metadata = parse_json_object(row.metadata_json)
                if not is_patient_visible_notification_metadata(metadata):
                    continue
                visible.append(_notification_view(session, row, metadata))
                if len(visible) >= limit:
                    break
            if len(visible) >= limit or len(rows) < batch_size:
                break
        visible.reverse()

    return {
        "notifications": visible,
        "last_seen_id": last_seen_id,
        "current_time": as_seoul_iso(clock.current_time),
    }


def notification_detail(
    session: Session,
    notification_id: str,
) -> dict[str, Any] | None:
    settings = get_settings()
    row = session.scalar(
        select(Notification).where(
            Notification.public_id == notification_id,
            Notification.patient_id == settings.patient_id,
        )
    )
    if row is None:
        return None
    metadata = parse_json_object(row.metadata_json)
    if not is_patient_visible_notification_metadata(metadata):
        return None
    return _notification_view(session, row, metadata)


def acknowledge_all_notifications(session: Session) -> int:
    settings = get_settings()
    clock = ensure_clock(session)
    rows = list(
        session.scalars(
            select(Notification).where(
                Notification.patient_id == settings.patient_id,
                Notification.visible_at <= clock.current_time,
                Notification.acknowledged.is_(False),
                Notification.notification_type != "system_policy_request",
            )
        ).all()
    )
    count = 0
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if not is_patient_visible_notification_metadata(metadata):
            continue
        if (
            row.notification_type == "conversation_alert"
            and metadata.get("category") == "missed_dose"
            and metadata.get("status") in {"awaiting_agent", "agent_ready"}
        ):
            continue
        row.acknowledged = True
        count += 1
    session.flush()
    return count


def chat_history_page(
    session: Session,
    *,
    before_date: date | None = None,
    limit_days: int = 1,
    limit_turns: int = 50,
) -> dict[str, Any]:
    settings = get_settings()
    clock = ensure_clock(session)
    today = clock.current_time.date()
    page_size = max(1, limit_days)
    turn_limit = max(1, limit_turns)
    upper_date = before_date or (today + timedelta(days=1))
    upper_display_at = _seoul_date_start_as_utc_naive(upper_date)
    max_messages = max(
        _CHAT_HISTORY_QUERY_BATCH_SIZE,
        turn_limit * _CHAT_HISTORY_MESSAGES_PER_TURN,
    )
    rows, selected_dates = _bounded_chat_history_rows(
        session,
        patient_id=settings.patient_id,
        upper_display_at=upper_display_at,
        initial_date=today if before_date is None else None,
        limit_days=page_size,
        limit_turns=turn_limit,
        max_messages=max_messages,
    )
    source_message_ids, response_message_ids = _chat_message_links(
        session,
        rows,
    )
    available: OrderedDict[date, list[ChatMessage]] = OrderedDict()
    for row in rows:
        row_date = _display_message_at(row).date()
        available.setdefault(row_date, []).append(row)

    days: list[dict[str, Any]] = []
    for row_date in reversed(selected_dates):
        messages = available.get(row_date, [])
        days.append(
            {
                "date": row_date.isoformat(),
                "messages": [
                    _chat_message_view(
                        message,
                        source_message_id=source_message_ids.get(message.id),
                        response_message_id=response_message_ids.get(message.id),
                    )
                    for message in reversed(messages)
                ],
            }
        )

    oldest_date = selected_dates[-1] if selected_dates else None
    next_before_date = (
        oldest_date.isoformat()
        if oldest_date is not None
        and _chat_history_has_older_date(
            session,
            patient_id=settings.patient_id,
            oldest_date=oldest_date,
        )
        else None
    )
    return {
        "days": days,
        "next_before_date": next_before_date,
    }


def _bounded_chat_history_rows(
    session: Session,
    *,
    patient_id: str,
    upper_display_at: datetime,
    initial_date: date | None,
    limit_days: int,
    limit_turns: int,
    max_messages: int,
) -> tuple[list[ChatMessage], list[date]]:
    rows: list[ChatMessage] = []
    selected_dates = [initial_date] if initial_date is not None else []
    selected_date_set = set(selected_dates)
    user_turn_count = 0
    cursor_display_at: datetime | None = None
    cursor_id: int | None = None
    stop = False

    while len(rows) < max_messages and not stop:
        predicates = [
            ChatMessage.patient_id == patient_id,
            ChatMessage.display_at.is_not(None),
            ChatMessage.display_at < upper_display_at,
        ]
        if cursor_display_at is not None and cursor_id is not None:
            predicates.append(
                or_(
                    ChatMessage.display_at < cursor_display_at,
                    and_(
                        ChatMessage.display_at == cursor_display_at,
                        ChatMessage.id < cursor_id,
                    ),
                )
            )
        batch_limit = min(
            _CHAT_HISTORY_QUERY_BATCH_SIZE,
            max_messages - len(rows),
        )
        batch = list(
            session.scalars(
                select(ChatMessage)
                .where(*predicates)
                .order_by(
                    desc(ChatMessage.display_at),
                    desc(ChatMessage.id),
                )
                .limit(batch_limit)
            ).all()
        )
        if not batch:
            break

        for row in batch:
            row_date = _display_message_at(row).date()
            if row_date not in selected_date_set:
                if len(selected_dates) >= limit_days:
                    stop = True
                    break
                selected_dates.append(row_date)
                selected_date_set.add(row_date)
            if row.role == "user":
                if user_turn_count >= limit_turns:
                    rows = [
                        candidate
                        for candidate in rows
                        if candidate.reply_to_message_id != row.id
                    ]
                    stop = True
                    break
                user_turn_count += 1
            rows.append(row)
        cursor_display_at = batch[-1].display_at
        cursor_id = batch[-1].id
        if len(batch) < batch_limit:
            break

    retained_dates = [initial_date] if initial_date is not None else []
    retained_date_set = set(retained_dates)
    for row in rows:
        row_date = _display_message_at(row).date()
        if row_date in retained_date_set:
            continue
        retained_dates.append(row_date)
        retained_date_set.add(row_date)
    return rows, retained_dates


def _chat_history_has_older_date(
    session: Session,
    *,
    patient_id: str,
    oldest_date: date,
) -> bool:
    boundary = _seoul_date_start_as_utc_naive(oldest_date)
    return (
        session.scalar(
            select(ChatMessage.id)
            .where(
                ChatMessage.patient_id == patient_id,
                ChatMessage.display_at.is_not(None),
                ChatMessage.display_at < boundary,
            )
            .limit(1)
        )
        is not None
    )


def _seoul_date_start_as_utc_naive(value: date) -> datetime:
    return (
        datetime.combine(value, time.min, tzinfo=_SEOUL)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )


def _notification_view(
    session: Session,
    row: Notification,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    alert_category = str(metadata.get("category") or "").strip()
    alert_status = str(metadata.get("status") or "").strip()
    severity = str(metadata.get("severity") or "").strip().lower()
    if severity not in {"info", "reminder", "warning", "critical"}:
        severity = (
            "warning"
            if (
                row.notification_type in {"agent_error", "nutrition_alert"}
                or alert_status == "agent_error"
            )
            else "reminder"
        )
    interaction = None
    if (
        row.notification_type == "conversation_alert"
        and alert_category == "missed_dose"
        and alert_status in {"awaiting_agent", "agent_ready", "agent_error"}
    ):
        state = {
            "awaiting_agent": "pending",
            "agent_ready": "ready",
            "agent_error": "failed",
        }[alert_status]
        message_id = str(metadata.get("chat_message_id") or "").strip() or None
        interaction = {
            "kind": "open_chat",
            "state": state,
            "message_id": message_id if state == "ready" else None,
        }
    return {
        "id": row.public_id,
        "notification_type": row.notification_type,
        "title": row.title,
        "body": row.body,
        "visible_at": as_seoul_iso(row.visible_at),
        "visible_at_label": row.visible_at.strftime("%m-%d %H:%M"),
        "acknowledged": bool(row.acknowledged),
        "metadata": {"severity": severity},
        "interaction": interaction,
        "dose_status": _related_dose_status(session, row),
        "related_dose_event_id": _related_dose_public_id(session, row),
    }


def _related_dose_public_id(
    session: Session,
    row: Notification,
) -> str | None:
    if row.related_dose_event_id is None:
        return None
    return session.scalar(
        select(DoseEvent.public_id).where(
            DoseEvent.id == row.related_dose_event_id
        )
    )


def _related_dose_status(
    session: Session,
    row: Notification,
) -> str | None:
    if row.related_dose_event_id is None:
        return None
    return session.scalar(
        select(DoseEvent.status).where(
            DoseEvent.id == row.related_dose_event_id
        )
    )


def _chat_message_links(
    session: Session,
    rows: list[ChatMessage],
) -> tuple[dict[int, str], dict[int, str]]:
    """Resolve structured reply links from Backend-owned DB relationships."""

    rows_by_id = {row.id: row for row in rows}
    patient_id = rows[0].patient_id if rows else None
    missing_source_ids = {
        row.reply_to_message_id
        for row in rows
        if row.role == "user"
        and row.reply_to_message_id is not None
        and row.reply_to_message_id not in rows_by_id
    }
    if missing_source_ids:
        external_sources = session.scalars(
            select(ChatMessage).where(
                ChatMessage.id.in_(missing_source_ids),
                ChatMessage.patient_id == patient_id,
            )
        ).all()
        rows_by_id.update(
            (source.id, source)
            for source in external_sources
        )
    source_message_ids: dict[int, str] = {}
    replies_by_source_id: dict[int, list[tuple[ChatMessage, bool]]] = {}

    link_rows = list(rows)
    returned_source_ids = {
        row.id
        for row in rows
        if row.role == "assistant"
        and row.message_type in {"selection_box", "input_box"}
    }
    if returned_source_ids:
        external_replies = session.scalars(
            select(ChatMessage).where(
                ChatMessage.reply_to_message_id.in_(
                    returned_source_ids
                ),
                ChatMessage.patient_id == patient_id,
                ChatMessage.role == "user",
            )
        ).all()
        known_ids = {row.id for row in link_rows}
        link_rows.extend(
            reply
            for reply in external_replies
            if reply.id not in known_ids
        )

    for reply in link_rows:
        if reply.role != "user":
            continue
        uses_db_relation = reply.reply_to_message_id is not None
        source = (
            rows_by_id.get(reply.reply_to_message_id)
            if uses_db_relation
            else None
        )
        if (
            source is None
            or source.role != "assistant"
            or source.patient_id != reply.patient_id
            or source.message_type not in {"selection_box", "input_box"}
            or reply.message_type != source.message_type
        ):
            continue
        source_message_ids[reply.id] = source.public_id
        replies_by_source_id.setdefault(source.id, []).append(
            (reply, uses_db_relation)
        )

    response_message_ids: dict[int, str] = {}
    for source_id, reply_relations in replies_by_source_id.items():
        source = rows_by_id[source_id]
        metadata = parse_json_object(source.metadata_json)
        if metadata.get("pending_response_status") != "answered":
            continue
        db_replies = [
            reply
            for reply, uses_db_relation in reply_relations
            if uses_db_relation
        ]
        if len(db_replies) == 1:
            response_message_ids[source_id] = db_replies[0].public_id
            continue
        replies = db_replies or [
            reply for reply, _uses_db_relation in reply_relations
        ]
        response_hint = str(metadata.get("response_message_id") or "").strip()
        if response_hint:
            matched = [
                reply for reply in replies if reply.public_id == response_hint
            ]
        else:
            matched = replies
        if len(matched) == 1:
            response_message_ids[source_id] = matched[0].public_id

    return source_message_ids, response_message_ids


def _chat_message_view(
    message: ChatMessage,
    *,
    source_message_id: str | None,
    response_message_id: str | None,
) -> dict[str, Any]:
    payload = parse_json_object(message.message_payload_json)
    metadata = parse_json_object(message.metadata_json)
    pending_response_status = str(
        metadata.get("pending_response_status") or ""
    ).strip()
    feedback_status = feedback_status_from_metadata(message.metadata_json)
    return {
        "message_id": message.public_id,
        # The DB-generated PK is used only as an opaque, monotonic UI order.
        # Resource identity remains the role-specific public message ID.
        "sort_sequence": message.id,
        "role": message.role,
        "message_type": message.message_type,
        "message": message.content or None,
        "content": _chat_message_content_view(payload),
        "created_at": _display_message_at(message).isoformat(),
        "processing_status": pending_response_status or message.processing_status,
        "response_message_id": response_message_id,
        "source_message_id": source_message_id,
        **feedback_status,
    }


def _chat_message_content_view(
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if not payload:
        return None
    content = ChatMessageContent.model_validate(
        {
            "message_title": payload.get("message_title"),
            "text": payload.get("text"),
            "tables": payload.get("tables"),
            "selections": payload.get("selections"),
            "inputs": payload.get("inputs"),
        }
    )
    return content.model_dump(mode="json")


def _display_message_at(message: ChatMessage) -> datetime:
    if message.display_at is None:
        raise RuntimeError("chat_message_display_at_required")
    return naive_utc_as_seoul(message.display_at)
