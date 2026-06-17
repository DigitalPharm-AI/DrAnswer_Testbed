from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json as dump_metadata_json
from shared.json_utils import parse_json_object as parse_metadata_json
from shared.schemas import (
    AgentNotificationRequest,
    DoseTakenToolRequest,
    DoseTakenToolResult,
)
from system_app.models import AgentDecisionAudit, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.dose_event_service import mark_dose_taken
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message


def apply_agent_dose_taken_request(session: Session, payload: DoseTakenToolRequest) -> DoseTakenToolResult:
    trace_id = f"{payload.source_trace_id or 'agent'}:mark_dose_taken:{payload.source_event_type}:{payload.dose_event_id}"
    event = mark_dose_taken(session, payload.dose_event_id, taken_at=payload.taken_at)
    if event is None:
        result = DoseTakenToolResult(
            dose_event_id=payload.dose_event_id,
            status="not_found",
            message="해당 복약 기록을 찾지 못했습니다.",
        )
    else:
        result = DoseTakenToolResult(
            dose_event_id=event.id,
            status="taken",
            taken_at=event.taken_at,
            message=f"{event.slot_label} {event.medication_name} 복약을 완료로 기록했습니다.",
        )
    session.add(
        AgentDecisionAudit(
            trace_id=trace_id,
            agent_name="agent_tool_executor",
            prompt_version_id=payload.source_trace_id or "n/a",
            decision_type="mark_dose_taken",
            structured_payload=dump_metadata_json(payload.model_dump(mode="json")),
            human_summary=result.message,
            applied=result.status == "taken",
            error_message="" if result.status == "taken" else result.message,
            source_event_type="agent_dose_taken",
        )
    )
    session.commit()
    return result


def process_agent_notification_callback(session: Session, payload: AgentNotificationRequest) -> dict:
    if payload.idempotency_key:
        existing = (
            session.query(AgentDecisionAudit)
            .filter(
                AgentDecisionAudit.trace_id == payload.idempotency_key,
                AgentDecisionAudit.source_event_type == "agent_notification_callback",
            )
            .first()
        )
        if existing is not None:
            return {"status": "duplicate", "notification_id": parse_metadata_json(existing.structured_payload).get("notification_id")}

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
        session.add(
            AgentDecisionAudit(
                trace_id=payload.idempotency_key,
                agent_name="agent_tool_executor",
                prompt_version_id="n/a",
                decision_type="agent_notification_callback",
                structured_payload=dump_metadata_json({"notification_id": notification.id}),
                human_summary=payload.body,
                applied=True,
                error_message="",
                source_event_type="agent_notification_callback",
            )
        )
    session.commit()
    return {"status": "ok", "notification_id": notification.id}
