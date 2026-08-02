from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, time

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json
from shared.json_utils import parse_json_object as parse_metadata_json
from shared.settings import get_settings
from system_app.models import ChatMessage, DoseEvent, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.policy_service import missed_dose_due_at_for_event
from system_app.services.ui_time import as_seoul_datetime, as_seoul_iso

settings = get_settings()


def get_today_dose_events(session: Session, current_time: datetime) -> Sequence[DoseEvent]:
    return get_dose_events_for_date(session, current_time.date())


def get_dose_events_for_date(session: Session, target_date: date) -> Sequence[DoseEvent]:
    start = datetime.combine(target_date, time.min)
    end = datetime.combine(target_date, time.max)
    stmt = (
        select(DoseEvent)
        .where(DoseEvent.scheduled_for.between(start, end))
        .order_by(DoseEvent.scheduled_for.asc(), DoseEvent.medication_name.asc(), DoseEvent.id.asc())
    )
    return session.scalars(stmt).all()


def get_latest_reply_prompt(session: Session, current_time: datetime) -> str | None:
    notification = session.scalar(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.acknowledged.is_(False),
            Notification.visible_at <= current_time,
        )
        .order_by(Notification.visible_at.desc(), Notification.id.desc())
    )
    if notification:
        return notification.body
    return None

def add_chat_message(
    session: Session,
    role: str,
    content: str,
    sender_type: str,
    category: str = "chat",
    related_dose_event_id: int | None = None,
    metadata: dict | None = None,
    patient_id: str | None = None,
    ai_request_id: str = "",
    message_type: str = "text",
    message_payload: dict | None = None,
    reply_to_message_id: int | None = None,
    processing_status: str = "completed",
) -> ChatMessage:
    current_time = ensure_clock(session).current_time
    resolved_patient_id = patient_id or settings.patient_id
    resolved_metadata = metadata or {}
    display_at = current_time
    for key in ("display_message_at", "message_at"):
        raw_value = resolved_metadata.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        try:
            display_at = datetime.fromisoformat(raw_value)
        except ValueError:
            continue
        break
    display_at = (
        as_seoul_datetime(display_at)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    message = ChatMessage(
        patient_id=resolved_patient_id,
        ai_request_id=ai_request_id,
        role=role,
        sender_type=sender_type,
        category=category,
        message_type=message_type,
        content=content,
        message_payload_json=dump_json(message_payload or {}),
        reply_to_message_id=reply_to_message_id,
        processing_status=processing_status,
        created_at=current_time,
        display_at=display_at,
        conversation_at=display_at,
        related_dose_event_id=related_dose_event_id,
        metadata_json=dump_json(resolved_metadata),
    )
    session.add(message)
    session.flush()
    return message

CONVERSATION_ALERT_CHAT_CATEGORIES = ["missed_dose", "policy_confirmation", "side_effect_reminder_safety"]


def chat_message_for_conversation_alert(
    session: Session,
    notification_id: int,
) -> ChatMessage | None:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category.in_(CONVERSATION_ALERT_CHAT_CATEGORIES))
        .order_by(desc(ChatMessage.id))
        .limit(100)
    ).all()
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        payload = metadata.get("conversation_alert")
        if isinstance(payload, dict) and payload.get("notification_id") == notification_id:
            return row
    return None


def conversation_alert_chat_metadata(notification: Notification, metadata: dict | None = None) -> dict:
    alert_metadata = parse_metadata_json(notification.metadata_json)
    category = str(alert_metadata.get("category") or "missed_dose")
    base = dict(metadata or {})
    base["conversation_alert"] = {
        "notification_id": notification.id,
        "category": category,
        "status": str(alert_metadata.get("status") or "agent_ready"),
        "reply_mode": "chat",
    }
    return base

def ensure_chat_message_for_conversation_alert(
    session: Session,
    notification: Notification,
    *,
    content: str | None = None,
    metadata: dict | None = None,
) -> ChatMessage | None:
    alert_metadata = parse_metadata_json(notification.metadata_json)
    if notification.notification_type != "conversation_alert":
        return None
    if alert_metadata.get("category") != "missed_dose" or alert_metadata.get("status") != "agent_ready":
        return None
    existing = chat_message_for_conversation_alert(session, notification.id)
    if existing is not None:
        existing.patient_id = notification.patient_id
        existing_metadata = parse_metadata_json(existing.metadata_json)
        existing_metadata.setdefault(
            "display_message_at",
            as_seoul_iso(notification.visible_at),
        )
        existing.metadata_json = dump_json(existing_metadata)
        session.flush()
        return existing
    message_content = (content or notification.body or "").strip()
    if not message_content:
        return None
    return add_chat_message(
        session,
        role="assistant",
        content=message_content,
        sender_type="assistant",
        category="missed_dose",
        related_dose_event_id=notification.related_dose_event_id,
        metadata=conversation_alert_chat_metadata(
            notification,
            {
                **(metadata or {}),
                "display_message_at": as_seoul_iso(notification.visible_at),
            },
        ),
        patient_id=notification.patient_id,
    )

def conversation_alert_visible_at(session: Session, related_dose_event_id: int | None, fallback_time: datetime) -> datetime:
    if related_dose_event_id is None:
        return fallback_time
    event = session.get(DoseEvent, related_dose_event_id)
    if event is None:
        return fallback_time
    return missed_dose_due_at_for_event(session, event)
