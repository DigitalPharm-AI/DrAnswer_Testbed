from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import Notification
from system_app.services.clock_service import resume_simulation_clock_from_notification
from system_app.services.policy_confirmation import (
    handle_policy_confirmation_reply,
    policy_confirmation_action,
    resolve_multiple_choice_reply,
)
from system_app.services.timeline_service import add_chat_message

POLICY_CONFIRMATION_NOT_FOUND = "notification_not_found"
POLICY_CONFIRMATION_WRONG_CATEGORY = "notification_does_not_accept_policy_confirmation"
POLICY_CONFIRMATION_STALE = "policy_confirmation_stale"
POLICY_CONFIRMATION_INVALID_ACTION = "policy_confirmation_invalid_action"
POLICY_CONFIRMATION_ERROR = "policy_confirmation_error"


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


def _resolved_policy_confirmation_action(message: str, metadata: dict) -> str | None:
    action = resolve_multiple_choice_reply(message, metadata.get("multiple_choice")) or policy_confirmation_action(message)
    if action is None:
        return None

    multiple_choice = metadata.get("multiple_choice")
    if not isinstance(multiple_choice, dict):
        return action
    options = multiple_choice.get("options")
    if not isinstance(options, list):
        return action
    allowed_actions = {
        str(option.get("value") or "").strip()
        for option in options
        if isinstance(option, dict) and str(option.get("value") or "").strip()
    }
    if allowed_actions and action not in allowed_actions:
        return None
    return action


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
                "reply_completed_at": utc_now().isoformat(),
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
    if notification is None or notification.patient_id != get_settings().patient_id:
        return False, POLICY_CONFIRMATION_NOT_FOUND
    notification_metadata = parse_json_object(notification.metadata_json)
    if notification.notification_type != "conversation_alert" or notification_metadata.get("category") != "policy_confirmation":
        return False, POLICY_CONFIRMATION_WRONG_CATEGORY

    action = _resolved_policy_confirmation_action(message, notification_metadata)
    if notification_metadata.get("status") == "reply_completed":
        if action is None:
            return False, POLICY_CONFIRMATION_INVALID_ACTION
        completed_action = str(notification_metadata.get("reply_action") or "").strip()
        if not completed_action:
            completed_action = _resolved_policy_confirmation_action(
                str(notification_metadata.get("patient_reply") or ""),
                notification_metadata,
            ) or ""
        if completed_action and action != completed_action:
            return False, POLICY_CONFIRMATION_STALE
        result_message = str(
            notification_metadata.get("reply_result_message")
            or notification_metadata.get("agent_reply")
            or "이미 처리된 정책 확인 요청입니다."
        )
        return bool(notification_metadata.get("reply_applied", False)), result_message

    if notification.acknowledged or notification_metadata.get("status") != "agent_ready":
        return False, POLICY_CONFIRMATION_STALE
    if action is None:
        return False, POLICY_CONFIRMATION_INVALID_ACTION

    try:
        with session.begin_nested():
            update_conversation_alert_reply_state(session, notification_id, status="reply_submitted", patient_reply=message)
            applied, result_message = handle_policy_confirmation_reply(session, notification, action)
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
            completed_metadata = parse_json_object(notification.metadata_json)
            completed_metadata["reply_action"] = action
            completed_metadata["reply_applied"] = applied
            notification.metadata_json = dump_json(completed_metadata)
            acknowledge_notification(session, notification_id, resume_conversation_clock=True, commit=False)
        session.commit()
        return applied, result_message
    except Exception:
        session.rollback()
        # Keep the prompt in its actionable state. The route returns HTTP 500,
        # so htmx preserves the current form and the same selection can be
        # retried without first repairing notification metadata.
        return False, POLICY_CONFIRMATION_ERROR
