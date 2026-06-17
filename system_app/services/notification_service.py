from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json
from shared.settings import get_settings
from system_app.models import Notification

settings = get_settings()


def create_notification(
    session: Session,
    notification_type: str,
    title: str,
    body: str,
    visible_at: datetime,
    related_dose_event_id: int | None = None,
    metadata: dict | None = None,
) -> Notification:
    notification = Notification(
        patient_id=settings.patient_id,
        notification_type=notification_type,
        title=title,
        body=body,
        visible_at=visible_at,
        related_dose_event_id=related_dose_event_id,
        metadata_json=dump_json(metadata or {}),
    )
    session.add(notification)
    session.flush()
    return notification


def get_unacknowledged_conversation_alert(session: Session, related_dose_event_id: int | None) -> Notification | None:
    if related_dose_event_id is None:
        return None
    return session.scalar(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.related_dose_event_id == related_dose_event_id,
            Notification.acknowledged.is_(False),
        )
        .order_by(desc(Notification.created_at), desc(Notification.id))
    )


def get_unacknowledged_conversation_alerts(session: Session, related_dose_event_id: int | None) -> list[Notification]:
    if related_dose_event_id is None:
        return []
    return list(
        session.scalars(
            select(Notification)
            .where(
                Notification.notification_type == "conversation_alert",
                Notification.related_dose_event_id == related_dose_event_id,
                Notification.acknowledged.is_(False),
            )
            .order_by(desc(Notification.created_at), desc(Notification.id))
        ).all()
    )


def acknowledge_duplicate_conversation_alerts(
    session: Session,
    related_dose_event_id: int | None,
    keep_notification_id: int,
) -> int:
    acknowledged = 0
    for notification in get_unacknowledged_conversation_alerts(session, related_dose_event_id):
        if notification.id == keep_notification_id:
            continue
        notification.acknowledged = True
        acknowledged += 1
    if acknowledged:
        session.flush()
    return acknowledged
