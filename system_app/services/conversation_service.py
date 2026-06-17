from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse
from system_app.models import Notification
from system_app.services.clock_service import resume_simulation_clock_from_notification
from system_app.services.policy_confirmation import handle_policy_confirmation_reply
from system_app.services.timeline_service import add_chat_message


def acknowledge_notification(
    session: Session,
    notification_id: int,
    *,
    resume_conversation_clock: bool = False,
    commit: bool = True,
) -> None:
    notification = session.get(Notification, notification_id)
    if notification:
        notification.acknowledged = True
        if resume_conversation_clock:
            resume_simulation_clock_from_notification(session, notification)
        if commit:
            session.commit()
        else:
            session.flush()

def update_conversation_alert_reply_state(
    session: Session,
    notification_id: int | None,
    *,
    status: str,
    patient_reply: str | None = None,
    agent_reply: str | None = None,
    result_message: str | None = None,
    response: AgentResponse | None = None,
) -> None:
    if notification_id is None:
        return
    notification = session.get(Notification, notification_id)
    if notification is None or notification.notification_type != "conversation_alert":
        return

    metadata = parse_json_object(notification.metadata_json)
    metadata["status"] = status
    if patient_reply is not None:
        metadata["patient_reply"] = patient_reply
    if agent_reply is not None:
        metadata["agent_reply"] = agent_reply
    if result_message is not None:
        metadata["reply_result_message"] = result_message
    if response is not None:
        metadata.update(
            {
                "reply_trace_id": response.trace_id,
                "reply_decision_type": response.decision_type,
                "reply_agent_name": response.agent_name,
                "reply_completed_at": datetime.utcnow().isoformat(),
            }
        )
    notification.metadata_json = dump_json(metadata)
    session.flush()

def handle_policy_confirmation_message(
    session: Session,
    message: str,
    notification_id: int | None = None,
) -> tuple[bool, str]:
    notification = session.get(Notification, notification_id) if notification_id is not None else None
    if notification is None:
        return False, "notification_not_found"
    notification_metadata = parse_json_object(notification.metadata_json)
    if notification.notification_type != "conversation_alert" or notification_metadata.get("category") != "policy_confirmation":
        return False, "notification_does_not_accept_policy_confirmation"
    try:
        with session.begin_nested():
            update_conversation_alert_reply_state(session, notification_id, status="reply_submitted", patient_reply=message)
            applied, result_message = handle_policy_confirmation_reply(session, notification, message)
            add_chat_message(
                session,
                role="assistant",
                content=result_message,
                sender_type="assistant",
                category="policy_confirmation",
                related_dose_event_id=notification.related_dose_event_id,
            )
            update_conversation_alert_reply_state(
                session,
                notification_id,
                status="reply_completed",
                patient_reply=message,
                agent_reply=result_message,
                result_message=result_message,
            )
            acknowledge_notification(session, notification_id, resume_conversation_clock=True, commit=False)
        session.commit()
        return applied, result_message
    except Exception:
        session.rollback()
        error_reply = "정책 확인 답변을 처리하지 못했습니다. 잠시 후 다시 시도해주세요."
        update_conversation_alert_reply_state(
            session,
            notification_id,
            status="reply_error",
            patient_reply=message,
            agent_reply=error_reply,
        )
        session.commit()
        return False, "policy_confirmation_error"
