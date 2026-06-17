from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.settings import get_settings
from system_app.models import ChatMessage, Notification, SystemPolicyOverride
from system_app.services.clock_service import pause_simulation_clock_for_conversation, resume_simulation_clock_from_notification
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message

settings = get_settings()

SIDE_EFFECT_REMINDER_SAFETY_CATEGORY = "side_effect_reminder_safety"
SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY = "side_effect_reminder_suppressed"
SIDE_EFFECT_REMINDER_SAFETY_MESSAGE = (
    "부작용에 대해 기록했습니다. 증상이 너무 심하다고 느껴지면 병원에 내원해보는 것이 좋겠습니다. "
    "복약 관련해서는 의료진과 충분한 상담이 필요하니 병원에 내원해서 다시 상담을 받아보는 게 어떨까요? "
    "기존 설정되었던 복약 알림은 유지할까요, 아니면 복약 알림과 미복용 AI 알림을 모두 끌까요?"
)
SIDE_EFFECT_REMINDER_SAFETY_OPTIONS = [
    {"action": "keep", "label": "알림 유지하기"},
    {"action": "suppress", "label": "알림 모두 끄기"},
]


def is_reminder_suppressed_after_side_effect(session: Session) -> bool:
    override = session.scalar(
        select(SystemPolicyOverride)
        .where(
            SystemPolicyOverride.patient_id == settings.patient_id,
            SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            SystemPolicyOverride.active.is_(True),
        )
        .order_by(desc(SystemPolicyOverride.updated_at), desc(SystemPolicyOverride.created_at), desc(SystemPolicyOverride.id))
    )
    if override is None:
        return False
    return override.value.strip().lower() == "true"


def set_reminder_suppressed_after_side_effect(
    session: Session,
    suppressed: bool,
    *,
    reason: str = "PRO-CTCAE 부작용 기록 후 환자 알림 안전 확인 응답",
) -> None:
    active_rows = session.scalars(
        select(SystemPolicyOverride).where(
            SystemPolicyOverride.patient_id == settings.patient_id,
            SystemPolicyOverride.policy_key == SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            SystemPolicyOverride.active.is_(True),
        )
    ).all()
    for row in active_rows:
        row.active = False
        row.updated_at = datetime.utcnow()
    session.add(
        SystemPolicyOverride(
            patient_id=settings.patient_id,
            policy_key=SIDE_EFFECT_REMINDER_SUPPRESSED_POLICY_KEY,
            value="true" if suppressed else "false",
            reason=reason,
            source="patient_request",
            active=True,
        )
    )
    session.flush()


def _existing_safety_prompt(session: Session, source_chat_message_id: int) -> Notification | None:
    rows = session.scalars(
        select(Notification)
        .where(Notification.notification_type == "conversation_alert")
        .order_by(desc(Notification.created_at), desc(Notification.id))
        .limit(200)
    ).all()
    for notification in rows:
        metadata = parse_json_object(notification.metadata_json)
        if metadata.get("category") != SIDE_EFFECT_REMINDER_SAFETY_CATEGORY:
            continue
        if metadata.get("source_chat_message_id") == source_chat_message_id:
            return notification
    return None


def create_side_effect_reminder_safety_prompt(session: Session, source_chat_message_id: int) -> Notification:
    existing = _existing_safety_prompt(session, source_chat_message_id)
    if existing is not None:
        return existing

    clock, resume_state = pause_simulation_clock_for_conversation(session)
    notification = create_notification(
        session,
        notification_type="conversation_alert",
        title="부작용 기록 후 알림 확인",
        body=SIDE_EFFECT_REMINDER_SAFETY_MESSAGE,
        visible_at=clock.current_time,
        metadata={
            "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
            "status": "agent_ready",
            "source_chat_message_id": source_chat_message_id,
            "resume_clock": resume_state,
            "options": SIDE_EFFECT_REMINDER_SAFETY_OPTIONS,
        },
    )
    add_chat_message(
        session,
        role="assistant",
        content=SIDE_EFFECT_REMINDER_SAFETY_MESSAGE,
        sender_type="assistant",
        category=SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
        metadata={
            "conversation_alert": {
                "notification_id": notification.id,
                "category": SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
                "status": "agent_ready",
                "reply_mode": "choice",
            },
            SIDE_EFFECT_REMINDER_SAFETY_CATEGORY: {
                "notification_id": notification.id,
                "source_chat_message_id": source_chat_message_id,
                "options": SIDE_EFFECT_REMINDER_SAFETY_OPTIONS,
            },
        },
    )
    session.flush()
    return notification


def _sync_safety_prompt_chat_metadata(session: Session, notification_id: int, metadata: dict) -> None:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY)
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(50)
    ).all()
    for message in rows:
        message_metadata = parse_json_object(message.metadata_json)
        safety_payload = message_metadata.get(SIDE_EFFECT_REMINDER_SAFETY_CATEGORY)
        if not isinstance(safety_payload, dict) or safety_payload.get("notification_id") != notification_id:
            continue
        alert_payload = message_metadata.get("conversation_alert")
        if isinstance(alert_payload, dict):
            alert_payload["status"] = metadata.get("status", alert_payload.get("status"))
            alert_payload["reply_mode"] = alert_payload.get("reply_mode", "choice")
        safety_payload.update(
            {
                "status": metadata.get("status", ""),
                "action": metadata.get("action", ""),
                "patient_reply": metadata.get("patient_reply", ""),
                "agent_reply": metadata.get("agent_reply", ""),
            }
        )
        message.metadata_json = dump_json(message_metadata)


def _add_duplicate_safety_reply_feedback(session: Session, notification_id: int) -> None:
    rows = session.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.category == SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
            ChatMessage.content == "이미 처리된 알림입니다.",
        )
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(20)
    ).all()
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("notification_id") == notification_id and metadata.get("duplicate_reply") is True:
            return
    add_chat_message(
        session,
        role="assistant",
        content="이미 처리된 알림입니다.",
        sender_type="assistant",
        category=SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
        metadata={"notification_id": notification_id, "duplicate_reply": True},
    )


def _side_effect_safety_action(message: str) -> str | None:
    payload = parse_json_object(message)
    if payload:
        action = payload.get("action") or payload.get("value")
        if isinstance(action, str):
            message = action
    compact = message.replace(" ", "").strip().lower()
    if compact in {"keep", "유지", "알림유지", "알림유지하기", "기존유지", "그대로", "취소"}:
        return "keep"
    if compact in {"suppress", "off", "끄기", "알림끄기", "알림모두끄기", "모두끄기", "다끌래", "다꺼줘"}:
        return "suppress"
    return None


def handle_side_effect_reminder_safety_reply(
    session: Session,
    notification_id: int | None,
    action: str,
) -> tuple[bool, str]:
    notification = session.get(Notification, notification_id) if notification_id is not None else None
    if notification is None:
        return False, "notification_not_found"
    metadata = parse_json_object(notification.metadata_json)
    if notification.notification_type != "conversation_alert" or metadata.get("category") != SIDE_EFFECT_REMINDER_SAFETY_CATEGORY:
        return False, "notification_does_not_accept_side_effect_reminder_safety"
    if metadata.get("status") == "reply_completed":
        _sync_safety_prompt_chat_metadata(session, notification.id, metadata)
        _add_duplicate_safety_reply_feedback(session, notification.id)
        return is_reminder_suppressed_after_side_effect(session), "이미 처리된 알림입니다."

    resolved_action = _side_effect_safety_action(action)
    if resolved_action is None:
        return False, "알림 유지 또는 끄기 중 하나를 선택해주세요."

    suppressed = resolved_action == "suppress"
    set_reminder_suppressed_after_side_effect(session, suppressed)
    patient_reply = "알림 모두 끄기" if suppressed else "알림 유지하기"
    result_message = (
        "복약 알림과 미복용 AI 알림을 모두 껐습니다. 복약 재개나 조정은 의료진과 상담 후 다시 설정해주세요."
        if suppressed
        else "기존 복약 알림과 미복용 AI 알림을 유지합니다."
    )
    metadata.update(
        {
            "status": "reply_completed",
            "action": resolved_action,
            "patient_reply": patient_reply,
            "agent_reply": result_message,
            "reply_result_message": result_message,
            "reply_completed_at": datetime.utcnow().isoformat(),
        }
    )
    notification.metadata_json = dump_json(metadata)
    notification.acknowledged = True
    resume_simulation_clock_from_notification(session, notification)
    _sync_safety_prompt_chat_metadata(session, notification.id, metadata)
    add_chat_message(
        session,
        role="assistant",
        content=result_message,
        sender_type="assistant",
        category=SIDE_EFFECT_REMINDER_SAFETY_CATEGORY,
    )
    session.flush()
    return suppressed, result_message
