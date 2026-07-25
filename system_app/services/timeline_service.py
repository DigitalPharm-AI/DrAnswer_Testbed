from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date, datetime, time

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json
from shared.json_utils import parse_json_object as parse_metadata_json
from shared.schemas import ChatTurn
from shared.settings import get_settings
from system_app.models import ChatMessage, DoseEvent, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.policy_service import missed_dose_due_at_for_event

settings = get_settings()
REMOVED_WELCOME_MESSAGE = "복약 정보를 입력하면 PHR 등록을 완료한 뒤 시뮬레이션을 시작할 수 있어요. 시간 진행과 배속 재생으로 시나리오를 빠르게 검증해보세요."
HIDDEN_DELIVERY_CHANNELS = {"chat_only", "internal_only"}


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


def dose_calendar_month_view(session: Session, selected_date: date, current_date: date) -> dict:
    month_start = selected_date.replace(day=1)
    _, last_day = calendar.monthrange(selected_date.year, selected_date.month)
    month_end = selected_date.replace(day=last_day)
    rows = session.scalars(
        select(DoseEvent).where(
            DoseEvent.scheduled_for >= datetime.combine(month_start, time.min),
            DoseEvent.scheduled_for <= datetime.combine(month_end, time.max),
        )
    ).all()
    counts_by_date: dict[date, dict[str, int]] = {}
    for row in rows:
        day = row.scheduled_for.date()
        counts = counts_by_date.setdefault(day, {"total": 0, "scheduled": 0, "taken": 0, "missed": 0})
        counts["total"] += 1
        counts[row.status] = counts.get(row.status, 0) + 1

    weeks = []
    for week in calendar.Calendar(firstweekday=6).monthdatescalendar(selected_date.year, selected_date.month):
        week_days = []
        for day in week:
            counts = counts_by_date.get(day, {"total": 0, "scheduled": 0, "taken": 0, "missed": 0})
            if counts["missed"]:
                status_class = "has-missed"
            elif counts["total"] and counts["taken"] == counts["total"]:
                status_class = "all-taken"
            elif counts["total"]:
                status_class = "has-scheduled"
            else:
                status_class = "empty"
            week_days.append(
                {
                    "date": day,
                    "iso_date": day.isoformat(),
                    "day": day.day,
                    "in_month": day.month == selected_date.month,
                    "is_today": day == current_date,
                    "is_selected": day == selected_date,
                    "counts": counts,
                    "status_class": status_class,
                }
            )
        weeks.append(week_days)
    return {
        "month_label": selected_date.strftime("%Y년 %m월"),
        "weekday_labels": ["일", "월", "화", "수", "목", "금", "토"],
        "weeks": weeks,
    }

def get_notifications(session: Session, current_time: datetime, include_future: bool = False) -> Sequence[Notification]:
    stmt = select(Notification)
    if not include_future:
        stmt = stmt.where(Notification.visible_at <= current_time)
    stmt = stmt.order_by(Notification.visible_at.desc(), Notification.created_at.desc()).limit(50)
    return session.scalars(stmt).all()

def get_notification_history(session: Session, current_time: datetime) -> Sequence[Notification]:
    day_start = datetime.combine(current_time.date(), time.min)
    stmt = (
        select(Notification)
        .where(
            Notification.visible_at >= day_start,
            Notification.visible_at <= current_time,
            Notification.notification_type != "system_policy_request",
        )
        .order_by(Notification.visible_at.desc(), Notification.created_at.desc(), Notification.id.desc())
    )
    rows = session.scalars(stmt).all()
    return [row for row in rows if parse_metadata_json(row.metadata_json).get("delivery_channel") not in HIDDEN_DELIVERY_CHANNELS]

def get_chat_messages(session: Session, limit: int = 50) -> Sequence[ChatMessage]:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.content != REMOVED_WELCOME_MESSAGE)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    ).all()
    return list(reversed(rows))

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

def get_recent_chat_turns(session: Session, limit: int = 50) -> list[ChatTurn]:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.content != REMOVED_WELCOME_MESSAGE)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    ).all()
    return [ChatTurn(role=row.role if row.role in {"user", "assistant", "system"} else "user", content=row.content, created_at=row.created_at) for row in reversed(rows)]


def get_recent_diet_recommendation_groups(
    session: Session,
    *,
    patient_id: str | None = None,
    limit: int = 3,
    message_scan_limit: int = 30,
) -> list[dict]:
    rows = session.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.patient_id == (patient_id or settings.patient_id),
            ChatMessage.role == "assistant",
            ChatMessage.content != REMOVED_WELCOME_MESSAGE,
        )
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(message_scan_limit)
    ).all()
    groups: list[dict] = []
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        payload = metadata.get("diet_recommendations")
        if not isinstance(payload, dict):
            continue
        recommendations = payload.get("recommendations")
        if not isinstance(recommendations, list) or not recommendations:
            continue
        compact_recommendations = [
            _compact_diet_recommendation(item, index)
            for index, item in enumerate(recommendations[:10], start=1)
            if isinstance(item, dict)
        ]
        compact_recommendations = [item for item in compact_recommendations if item.get("food_name") or item.get("food_ref_id")]
        if not compact_recommendations:
            continue
        groups.append(
            {
                "chat_message_id": row.id,
                "created_at": row.created_at.isoformat(),
                "constraints_applied": payload.get("constraints_applied", {}),
                "meal_type_requested": payload.get("meal_type_requested", ""),
                "recommendations": compact_recommendations,
            }
        )
        if len(groups) >= limit:
            break
    return groups


def _compact_diet_recommendation(item: dict, index: int) -> dict:
    return {
        "index": index,
        "food_ref_id": item.get("food_ref_id", ""),
        "food_name": item.get("food_name", ""),
        "category": item.get("category", ""),
        "serving_size": item.get("serving_size", 0),
        "nutrient_basis": item.get("nutrient_basis", ""),
        "nutrients": item.get("nutrients", {}),
        "recommendation_reasons": item.get("recommendation_reasons", []),
        "recommendation_fit": item.get("recommendation_fit", ""),
    }


def get_recent_chat_turns_for_dose_event(session: Session, related_dose_event_id: int, limit: int = 10) -> list[ChatTurn]:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.related_dose_event_id == related_dose_event_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
    ).all()
    return [ChatTurn(role=row.role if row.role in {"user", "assistant", "system"} else "user", content=row.content, created_at=row.created_at) for row in reversed(rows)]

def add_chat_message(
    session: Session,
    role: str,
    content: str,
    sender_type: str,
    category: str = "chat",
    related_dose_event_id: int | None = None,
    metadata: dict | None = None,
    patient_id: str | None = None,
    conversation_id: str = "",
    ai_request_id: str = "",
    message_type: str = "text",
    message_payload: dict | None = None,
    reply_to_message_id: int | None = None,
    processing_status: str = "completed",
) -> ChatMessage:
    current_time = ensure_clock(session).current_time
    message = ChatMessage(
        patient_id=patient_id or settings.patient_id,
        conversation_id=conversation_id,
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
        related_dose_event_id=related_dose_event_id,
        metadata_json=dump_json(metadata or {}),
    )
    session.add(message)
    session.flush()
    return message

CONVERSATION_ALERT_CHAT_CATEGORIES = ["missed_dose", "policy_confirmation", "side_effect_reminder_safety"]


def chat_message_for_conversation_alert_exists(session: Session, notification_id: int) -> bool:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category.in_(CONVERSATION_ALERT_CHAT_CATEGORIES))
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(100)
    ).all()
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        payload = metadata.get("conversation_alert")
        if isinstance(payload, dict) and payload.get("notification_id") == notification_id:
            return True
    return False

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
    if chat_message_for_conversation_alert_exists(session, notification.id):
        return None
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
        metadata=conversation_alert_chat_metadata(notification, metadata),
    )

def conversation_alert_visible_at(session: Session, related_dose_event_id: int | None, fallback_time: datetime) -> datetime:
    if related_dose_event_id is None:
        return fallback_time
    event = session.get(DoseEvent, related_dose_event_id)
    if event is None:
        return fallback_time
    return missed_dose_due_at_for_event(session, event)
