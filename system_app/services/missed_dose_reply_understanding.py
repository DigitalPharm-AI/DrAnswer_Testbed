from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from system_app.models import Notification

MISSED_DOSE_REPLY_METADATA_KEY = "missed_dose_reply"
PENDING_AGENT_INTERPRETATION = "pending_agent_interpretation"
AGENT_RESPONSE_COMPLETED = "agent_response_completed"


def missed_dose_reply_request_metadata(
    notification: Notification,
) -> dict[str, Any]:
    """Build neutral context without inferring intent from the user's text."""

    return {
        MISSED_DOSE_REPLY_METADATA_KEY: {
            "conversation_alert_id": notification.id,
            "related_dose_event_id": notification.related_dose_event_id,
            "status": PENDING_AGENT_INTERPRETATION,
        }
    }


def complete_missed_dose_reply(
    session: Session,
    *,
    notification_id: int | None,
    assistant_message_id: str,
) -> None:
    """Close the alert only after the real Agent response was persisted."""

    if notification_id is None:
        return
    notification = session.get(Notification, notification_id)
    if (
        notification is None
        or notification.notification_type != "conversation_alert"
    ):
        return
    metadata = parse_json_object(notification.metadata_json)
    if metadata.get("category") != "missed_dose":
        return
    metadata["status"] = AGENT_RESPONSE_COMPLETED
    metadata["assistant_message_id"] = assistant_message_id
    notification.metadata_json = dump_json(metadata)
    notification.acknowledged = True
    session.flush()
