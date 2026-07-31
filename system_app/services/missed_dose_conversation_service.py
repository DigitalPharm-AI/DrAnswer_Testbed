from __future__ import annotations

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from shared.settings import get_settings
from system_app.models import ChatMessage, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.missed_dose_flag_service import (
    is_active_missed_dose_flag_for_event,
)


def active_missed_dose_conversation_alert(
    session: Session,
    *,
    include_acknowledged: bool = False,
) -> Notification | None:
    """Return the latest unresolved missed-dose prompt for the testbed patient.

    Notification acknowledgement represents whether the alert was read.  The
    conversation remains unresolved until its metadata status changes from
    ``agent_ready`` after the patient submits a reply.
    """

    settings = get_settings()
    clock = ensure_clock(session)
    predicates = [
        Notification.patient_id == settings.patient_id,
        Notification.notification_type == "conversation_alert",
        Notification.visible_at <= clock.current_time,
    ]
    if not include_acknowledged:
        predicates.append(Notification.acknowledged.is_(False))

    rows = session.scalars(
        select(Notification)
        .where(*predicates)
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(20)
    ).all()
    for notification in rows:
        metadata = parse_json_object(notification.metadata_json)
        if (
            metadata.get("category") == "missed_dose"
            and metadata.get("status") == "agent_ready"
            and is_active_missed_dose_flag_for_event(
                session,
                notification.related_dose_event_id,
            )
        ):
            return notification
    return None


def missed_dose_conversation_alert_for_message(
    session: Session,
    source_message_id: str,
) -> tuple[Notification, ChatMessage] | None:
    """Resolve a patient-visible missed-dose prompt without exposing DB IDs."""

    settings = get_settings()
    message = session.scalar(
        select(ChatMessage).where(
            ChatMessage.public_id == source_message_id,
            ChatMessage.patient_id == settings.patient_id,
            ChatMessage.category == "missed_dose",
        )
    )
    if message is None:
        return None
    message_metadata = parse_json_object(message.metadata_json)
    alert_metadata = message_metadata.get("conversation_alert")
    if not isinstance(alert_metadata, dict):
        return None
    raw_notification_id = alert_metadata.get("notification_id")
    if not isinstance(raw_notification_id, int):
        return None
    notification = session.get(Notification, raw_notification_id)
    if (
        notification is None
        or notification.patient_id != settings.patient_id
        or notification.notification_type != "conversation_alert"
    ):
        return None
    notification_metadata = parse_json_object(notification.metadata_json)
    if (
        notification_metadata.get("category") != "missed_dose"
        or notification_metadata.get("status") != "agent_ready"
        or not is_active_missed_dose_flag_for_event(
            session,
            notification.related_dose_event_id,
        )
    ):
        return None
    return notification, message
