from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json as dump_metadata_json
from shared.schemas import AgentNotificationRequest
from system_app.models import Notification
from system_app.services.backend_v13_service import BackendRequestGate
from system_app.services.clock_service import ensure_clock
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message


def process_agent_notification_callback(session: Session, payload: AgentNotificationRequest) -> dict:
    request_gate = BackendRequestGate()
    if payload.idempotency_key:
        replay = request_gate.begin(
            session,
            api_path="/api/agent/notifications",
            request_id=payload.idempotency_key,
            payload=payload,
        )
        if replay is not None:
            result = dict(replay.body)
            result["status"] = "duplicate"
            return result

    clock = ensure_clock(session)
    notification = session.get(Notification, payload.notification_id) if payload.notification_id is not None else None
    if notification is None:
        notification = create_notification(
            session,
            notification_type=payload.notification_type,
            title=payload.title,
            body=payload.body,
            visible_at=payload.visible_at or clock.current_time,
            related_dose_event_id=payload.related_dose_event_id,
            metadata=payload.metadata,
        )
    else:
        notification.title = payload.title
        notification.body = payload.body
        notification.notification_type = payload.notification_type
        notification.visible_at = payload.visible_at or notification.visible_at
        notification.metadata_json = dump_metadata_json(payload.metadata)
        session.flush()

    if payload.chat_category:
        add_chat_message(
            session,
            role="assistant",
            content=payload.body,
            sender_type="assistant",
            category=payload.chat_category,
            related_dose_event_id=payload.related_dose_event_id,
        )

    if payload.idempotency_key:
        request_gate.complete(
            session,
            api_path="/api/agent/notifications",
            request_id=payload.idempotency_key,
            status_code=200,
            body={
                "status": "ok",
                "notification_id": notification.id,
            },
        )
    session.commit()
    return {"status": "ok", "notification_id": notification.id}
