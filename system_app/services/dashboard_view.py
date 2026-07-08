from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object as parse_metadata_json
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import AgentJob, ChatMessage, DoseEvent, Notification, ReminderPolicy
from system_app.services.agent_client import AgentClient
from system_app.services.clock_service import ensure_clock
from system_app.services.medication_plan_service import get_schedule_map, list_medication_plans
from system_app.services.nutrition_service import nutrition_dashboard_view
from system_app.services.patient_profile_service import phr_profile_view, simulation_readiness
from system_app.services.policy_service import daily_pattern_conversation_time_view, policy_missed_dose_delay_minutes, resolve_policy_for_slot
from system_app.services.side_effect_reminder_safety import is_reminder_suppressed_after_side_effect
from system_app.services.simulation_constants import (
    CUSTOM_CHOICE,
    DOSAGE_PRESET_OPTIONS,
    MEDICATION_PRESET_OPTIONS,
    SCHEDULE_TEMPLATE_MAP,
)
from system_app.services.timeline_service import (
    chat_message_for_conversation_alert_exists,
    dose_calendar_month_view,
    get_chat_messages,
    get_dose_events_for_date,
    get_notification_history,
)

settings = get_settings()
CORRUPTED_DISPLAY_VALUES = {"?", "??", "???", "????", "?????", "??????", "???D", "1?", "2?", "1??"}
SCHEDULE_PRESET_OPTIONS = [
    {
        "value": key,
        "label": value["label"],
        "times_label": " / ".join(value["times"]),
    }
    for key, value in SCHEDULE_TEMPLATE_MAP.items()
]
MEDICATION_FORM_OPTIONS = [{"value": value, "label": "직접 입력" if value == CUSTOM_CHOICE else value} for value in MEDICATION_PRESET_OPTIONS]
DOSAGE_FORM_OPTIONS = [{"value": value, "label": "직접 입력" if value == CUSTOM_CHOICE else value} for value in DOSAGE_PRESET_OPTIONS]


def display_text(value: str | None, fallback: str) -> str:
    text = (value or "").strip()
    if not text or text in CORRUPTED_DISPLAY_VALUES or set(text) == {"?"}:
        return fallback
    return text


def medication_plan_view(plan) -> dict:
    return {
        "id": plan.id,
        "medication_name": display_text(plan.medication_name, "약물명 확인 필요"),
        "dosage": display_text(plan.dosage, "용량 미입력") if plan.dosage else "",
        "start_date": plan.start_date,
        "end_date": plan.end_date,
        "instructions": plan.instructions,
        "raw": plan,
    }


CHAT_CATEGORY_LABELS = {
    "system_event": "대화",
    "multiturn_chat": "대화",
    "chat": "대화",
    "reply": "알림 답변",
    "missed_dose": "미복용 대화",
    "policy_confirmation": "정책 확인",
    "side_effect_reminder_safety": "부작용 알림 확인",
    "ae_pro_ctcae": "부작용 문항",
    "ae_response": "문항 응답",
    "nutrition": "영양 대화",
    "error": "오류",
}
PENDING_AGENT_CHAT_STATUSES = {"sent", "awaiting_agent"}
AGENT_CONVERSATION_CATEGORIES = {
    "system_event",
    "multiturn_chat",
    "reply",
    "missed_dose",
    "policy_confirmation",
    "side_effect_reminder_safety",
    "ae_pro_ctcae",
    "ae_response",
    "nutrition",
}
CHAT_PROMPT_CATEGORIES = {"missed_dose", "policy_confirmation", "side_effect_reminder_safety"}
NOTIFICATION_TYPE_LABELS = {
    "medication_alert": "복약 알림",
    "conversation_alert": "대화 요청",
    "nutrition_alert": "영양 알림",
    "agent_error": "AI 오류",
}
HIDDEN_DELIVERY_CHANNELS = {"chat_only", "internal_only"}
CHAT_SORT_SOURCE_MESSAGE = 0
CHAT_SORT_SOURCE_PROMPT = 1
CHAT_SORT_SOURCE_PENDING = 2
CHAT_SORT_SOURCE_HISTORY = 3


def is_patient_visible_notification_metadata(metadata: dict) -> bool:
    return metadata.get("delivery_channel") not in HIDDEN_DELIVERY_CHANNELS


def policy_chat_options(multiple_choice: dict) -> list[dict]:
    raw_options = multiple_choice.get("options") if isinstance(multiple_choice.get("options"), list) else []
    options = [
        option
        for option in raw_options
        if isinstance(option, dict) and option.get("value") in {"increase", "decrease", "keep"}
    ]
    if not options:
        options = [
            {"number": 1, "value": "increase", "label": "늘리기", "text": "1. 늘리기", "description": ""},
            {"number": 2, "value": "keep", "label": "현행 유지하기", "text": "2. 현행 유지하기", "description": ""},
        ]
    for index, option in enumerate(options, start=1):
        option.setdefault("number", index)
        if option.get("value") == "keep":
            option["label"] = "현행 유지하기"
            option["text"] = f"{option.get('number', index)}. 현행 유지하기"
        if option.get("value") == "decrease":
            option["label"] = "줄이기"
            option["text"] = f"{option.get('number', index)}. 줄이기"
    return options


def ae_prompt_view(metadata: dict, message_id: int) -> dict | None:
    payload = metadata.get("ae_pro_ctcae")
    if not isinstance(payload, dict):
        return None
    questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
    responses = payload.get("responses") if isinstance(payload.get("responses"), list) else []
    answered_indexes = {
        int(row.get("question_index"))
        for row in responses
        if isinstance(row, dict) and isinstance(row.get("question_index"), int)
    }
    return {
        "chat_message_id": message_id,
        "input_symptom": payload.get("input_symptom", ""),
        "matched": bool(payload.get("matched")),
        "match_type": payload.get("match_type", ""),
        "matched_symptom_term": payload.get("matched_symptom_term", ""),
        "matched_korean_symptom_name": payload.get("matched_korean_symptom_name", ""),
        "similarity": payload.get("similarity", 0.0),
        "threshold": payload.get("threshold", 0.0),
        "sheet_name": payload.get("sheet_name", ""),
        "questions": questions,
        "responses": responses,
        "answered_indexes": answered_indexes,
    }


def policy_confirmation_view(metadata: dict) -> dict | None:
    payload = metadata.get("policy_confirmation")
    if not isinstance(payload, dict):
        return None
    multiple_choice = payload.get("multiple_choice") if isinstance(payload.get("multiple_choice"), dict) else {}
    return {
        "notification_id": payload.get("notification_id"),
        "policy_change": payload.get("policy_change") if isinstance(payload.get("policy_change"), dict) else {},
        "multiple_choice": multiple_choice,
        "options": policy_chat_options(multiple_choice),
    }


def side_effect_reminder_safety_view(metadata: dict) -> dict | None:
    payload = metadata.get("side_effect_reminder_safety")
    if not isinstance(payload, dict):
        return None
    alert_payload = metadata.get("conversation_alert") if isinstance(metadata.get("conversation_alert"), dict) else {}
    status = payload.get("status") or alert_payload.get("status") or "agent_ready"
    if status != "agent_ready":
        return None
    raw_options = payload.get("options") if isinstance(payload.get("options"), list) else []
    options = [
        option
        for option in raw_options
        if isinstance(option, dict) and option.get("action") in {"keep", "suppress"} and option.get("label")
    ]
    if not options:
        options = [
            {"action": "keep", "label": "알림 유지하기"},
            {"action": "suppress", "label": "알림 모두 끄기"},
        ]
    return {
        "notification_id": payload.get("notification_id"),
        "options": options,
    }


def conversation_alert_action_view(metadata: dict) -> dict | None:
    payload = metadata.get("conversation_alert")
    if not isinstance(payload, dict):
        return None
    if not payload.get("notification_id"):
        return None
    return {
        "notification_id": payload.get("notification_id"),
        "category": payload.get("category", ""),
        "status": payload.get("status", ""),
        "reply_mode": payload.get("reply_mode", "chat"),
    }


def food_selection_view(metadata: dict, message_id: int) -> dict | None:
    payload = metadata.get("food_selection")
    if not isinstance(payload, dict):
        return None
    foods_queue = payload.get("foods_queue") if isinstance(payload.get("foods_queue"), list) else []
    confirmed_foods = payload.get("confirmed_foods") if isinstance(payload.get("confirmed_foods"), list) else []
    return {
        "chat_message_id": message_id,
        "stage": payload.get("stage", "awaiting_food_choice"),
        "query": payload.get("query", ""),
        "candidates": payload.get("candidates") if isinstance(payload.get("candidates"), list) else [],
        "selected_food": payload.get("selected_food"),
        "portion_g": payload.get("portion_g"),
        "meal_type": payload.get("meal_type"),
        "foods_queue": foods_queue,
        "confirmed_foods": confirmed_foods,
        "total_foods": len(confirmed_foods) + len(foods_queue) + 1,
        "current_index": len(confirmed_foods) + 1,
    }


def diet_recommendations_view(metadata: dict) -> dict | None:
    payload = metadata.get("diet_recommendations")
    if not isinstance(payload, dict):
        return None
    recommendations = payload.get("recommendations") if isinstance(payload.get("recommendations"), list) else []
    if not recommendations:
        return None
    return {
        "recommendations": recommendations,
        "constraints_applied": payload.get("constraints_applied") if isinstance(payload.get("constraints_applied"), dict) else {},
        "blocked_count": payload.get("blocked_count", 0),
        "total_candidates": payload.get("total_candidates", 0),
    }


def chat_message_view(message) -> dict:
    metadata = parse_metadata_json(getattr(message, "metadata_json", "{}"))
    from_user = message.role == "user" or message.sender_type in {"patient", "user"}
    from_ai = message.role == "assistant" or message.sender_type == "assistant"
    one_way_alert = message.category in {"missed_dose", "policy_confirmation", "side_effect_reminder_safety", "error"} and not from_user
    classes = [
        message.sender_type,
        message.category,
        "from-user" if from_user else "from-ai" if from_ai else "from-system",
        "one-way-alert" if one_way_alert else "multi-turn-bubble",
    ]
    return {
        "id": message.id,
        "sort_at": message.created_at,
        "sort_order": 0,
        "sort_source": CHAT_SORT_SOURCE_MESSAGE,
        "sort_sequence": message.id,
        "pending": False,
        "classes": " ".join(classes),
        "role_label": "User" if from_user else "AI" if from_ai else "알림",
        "category_label": CHAT_CATEGORY_LABELS.get(message.category, message.category),
        "content": message.content,
        "time_label": message.created_at.strftime("%m-%d %H:%M"),
        "conversation_alert": conversation_alert_action_view(metadata),
        "policy_confirmation": policy_confirmation_view(metadata),
        "side_effect_reminder_safety": side_effect_reminder_safety_view(metadata),
        "ae_pro_ctcae": ae_prompt_view(metadata, message.id),
        "food_selection": food_selection_view(metadata, message.id),
        "diet_recommendations": diet_recommendations_view(metadata),
    }

def is_hidden_policy_confirmation_reply(message) -> bool:
    if message.category != "reply":
        return False
    payload = parse_metadata_json(getattr(message, "content", ""))
    return payload.get("prompt_type") == "policy_confirmation"


def is_agent_conversation_message(message) -> bool:
    if is_hidden_policy_confirmation_reply(message):
        return False
    if message.category in AGENT_CONVERSATION_CATEGORIES:
        return True
    return message.category == "chat" and message.related_dose_event_id is None

def pending_agent_chat_views(session: Session, current_time: datetime) -> list[dict]:
    stale_after_seconds = settings.llm_timeout_seconds + 30
    now = utc_now()
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "system_policy_request",
            Notification.visible_at <= current_time,
        )
        .order_by(Notification.visible_at.asc(), Notification.id.asc())
        .limit(20)
    ).all()
    views = []
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        if metadata.get("category") != "agent_conversation":
            continue
        async_continuation_pending = metadata.get("async_continuation_status") == "pending"
        if metadata.get("status") not in PENDING_AGENT_CHAT_STATUSES and not async_continuation_pending:
            continue
        if row.created_at and (now - row.created_at).total_seconds() > stale_after_seconds:
            continue
        views.append(
            {
                "id": f"pending-agent-{row.id}",
                "sort_at": row.created_at,
                "sort_order": 1,
                "sort_source": CHAT_SORT_SOURCE_PENDING,
                "sort_sequence": row.id,
                "pending": True,
                "classes": "assistant multiturn_chat from-ai multi-turn-bubble pending-response",
                "role_label": "AI",
                "category_label": CHAT_CATEGORY_LABELS["multiturn_chat"],
                "content": "응답 생성 중",
                "time_label": row.created_at.strftime("%m-%d %H:%M"),
                "conversation_alert": None,
                "policy_confirmation": None,
                "side_effect_reminder_safety": None,
                "ae_pro_ctcae": None,
                "food_selection": None,
            }
        )
    return views

def notification_policy_confirmation_view(notification: Notification, metadata: dict) -> dict | None:
    if metadata.get("category") != "policy_confirmation":
        return None
    policy_change = metadata.get("policy_change") if isinstance(metadata.get("policy_change"), dict) else {}
    multiple_choice = metadata.get("multiple_choice") if isinstance(metadata.get("multiple_choice"), dict) else {}
    return {
        "notification_id": notification.id,
        "policy_change": policy_change,
        "multiple_choice": multiple_choice,
        "options": policy_chat_options(multiple_choice),
    }


def notification_side_effect_reminder_safety_view(notification: Notification, metadata: dict) -> dict | None:
    if metadata.get("category") != "side_effect_reminder_safety":
        return None
    if metadata.get("status") != "agent_ready":
        return None
    options = metadata.get("options") if isinstance(metadata.get("options"), list) else []
    return {
        "notification_id": notification.id,
        "options": options
        or [
            {"action": "keep", "label": "알림 유지하기"},
            {"action": "suppress", "label": "알림 모두 끄기"},
        ],
    }


def missing_chat_prompt_views(session: Session, current_time: datetime) -> list[dict]:
    views = []
    for notification in visible_chat_prompt_notifications(session, current_time):
        metadata = parse_metadata_json(notification.metadata_json)
        category = str(metadata.get("category") or "")
        if category not in CHAT_PROMPT_CATEGORIES:
            continue
        if chat_message_for_conversation_alert_exists(session, notification.id):
            continue
        content = (notification.body or "").strip()
        if not content:
            continue
        policy_confirmation = notification_policy_confirmation_view(notification, metadata)
        side_effect_safety = notification_side_effect_reminder_safety_view(notification, metadata)
        views.append(
            {
                "id": f"conversation-alert-{notification.id}",
                "sort_at": notification.created_at,
                "sort_order": 0,
                "sort_source": CHAT_SORT_SOURCE_PROMPT,
                "sort_sequence": notification.id,
                "pending": False,
                "classes": f"assistant {category} from-ai one-way-alert",
                "role_label": "AI",
                "category_label": CHAT_CATEGORY_LABELS.get(category, category),
                "content": content,
                "time_label": notification.created_at.strftime("%m-%d %H:%M"),
                "conversation_alert": {
                    "notification_id": notification.id,
                    "category": category,
                    "status": "agent_ready",
                    "reply_mode": "chat",
                },
                "policy_confirmation": policy_confirmation,
                "side_effect_reminder_safety": side_effect_safety,
                "ae_pro_ctcae": None,
                "food_selection": None,
            }
        )
    return views

def sorted_chat_views(views: list[dict]) -> list[dict]:
    views.sort(
        key=lambda row: (
            row["sort_at"],
            row["sort_order"],
            row.get("sort_source", CHAT_SORT_SOURCE_MESSAGE),
            row.get("sort_sequence") or 0,
        )
    )
    for row in views:
        row.pop("sort_at", None)
        row.pop("sort_order", None)
        row.pop("sort_source", None)
        row.pop("sort_sequence", None)
    return views

def agent_conversation_views(session: Session, current_time: datetime) -> list[dict]:
    views = [chat_message_view(message) for message in get_chat_messages(session) if is_agent_conversation_message(message)]
    views.extend(missing_chat_prompt_views(session, current_time))
    views.extend(pending_agent_chat_views(session, current_time))
    return sorted_chat_views(views)

def notification_history_chat_views(session: Session, current_time: datetime) -> list[dict]:
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.visible_at <= current_time,
            Notification.notification_type != "system_policy_request",
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(80)
    ).all()
    views = []
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        if not is_patient_visible_notification_metadata(metadata):
            continue
        body = (row.body or "").strip()
        content = f"{row.title}: {body}" if body else row.title
        views.append(
            {
                "id": f"notification-history-{row.id}",
                "sort_at": row.visible_at,
                "sort_order": 2,
                "sort_source": CHAT_SORT_SOURCE_HISTORY,
                "sort_sequence": row.id,
                "pending": False,
                "classes": f"system notification_history {row.notification_type} from-system one-way-alert",
                "role_label": "알림",
                "category_label": NOTIFICATION_TYPE_LABELS.get(row.notification_type, row.notification_type),
                "content": content,
                "time_label": row.visible_at.strftime("%m-%d %H:%M"),
                "conversation_alert": None,
                "policy_confirmation": None,
                "side_effect_reminder_safety": None,
                "ae_pro_ctcae": None,
                "food_selection": None,
            }
        )
    return views


def conversation_history_views(session: Session, current_time: datetime) -> list[dict]:
    views = [
        chat_message_view(message)
        for message in get_chat_messages(session)
        if not is_hidden_policy_confirmation_reply(message) and not is_agent_conversation_message(message)
    ]
    views.extend(notification_history_chat_views(session, current_time))
    return sorted_chat_views(views)


def fallback_agent_model_config_view() -> dict:
    return {
        "provider": settings.llm_provider,
        "model_tier": settings.llm_model_tier,
        "model_id": settings.model_id_for_tier(settings.llm_model_tier),
        "available_tiers": settings.available_model_tiers(),
    }

def policy_source_label(policy: ReminderPolicy) -> str:
    if policy.source == "pattern_analysis":
        return "맞춤 정책"
    if policy.source == "system_request":
        return "시스템 정책"
    if policy.source == "patient_request":
        return "요청 정책"
    return "맞춤 정책"

def policy_primary_reminder_label(timing: str | None, offset_minutes: int | None) -> str:
    timing = timing or "at"
    offset_minutes = offset_minutes or 0
    if timing == "before":
        return f"복약 {offset_minutes}분 전"
    if timing == "after":
        return f"복약 {offset_minutes}분 후"
    return "복약 정시"

def get_active_policies(session: Session, today: date) -> list[dict]:
    stmt = (
        select(ReminderPolicy)
        .where(
            ReminderPolicy.active.is_(True),
            ReminderPolicy.patient_id == settings.patient_id,
            ReminderPolicy.effective_start_date <= today,
            ReminderPolicy.effective_end_date >= today,
        )
        .order_by(desc(ReminderPolicy.created_at))
    )
    explicit_policies = session.scalars(stmt).all()
    views: list[dict] = []
    covered_slots: set[str] = set()
    for policy in explicit_policies:
        covered_slots.add(policy.slot_label)
        views.append(
            {
                "slot_label": policy.slot_label,
                "extra_reminders": policy.extra_reminders,
                "interval_minutes": policy.interval_minutes,
                "missed_dose_after_minutes": policy.missed_dose_after_minutes
                if policy.missed_dose_after_minutes is not None
                else policy_missed_dose_delay_minutes(policy.extra_reminders, policy.interval_minutes),
                "primary_reminder_label": policy_primary_reminder_label(
                    getattr(policy, "primary_reminder_timing", None),
                    getattr(policy, "primary_reminder_offset_minutes", None),
                ),
                "reason": policy.reason,
                "source_label": policy_source_label(policy),
                "period_label": f"{policy.effective_start_date.isoformat()} ~ {policy.effective_end_date.isoformat()}",
            }
        )

    active_slot_labels: set[str] = set()
    schedule_map = get_schedule_map(session)
    for plan in list_medication_plans(session):
        if plan.active and plan.start_date <= today <= plan.end_date:
            active_slot_labels.update(schedule.slot_label for schedule in schedule_map.get(plan.id, []))

    default_slot_labels = sorted(active_slot_labels - covered_slots)
    if not views and not default_slot_labels:
        default_slot_labels = ["기본 전체 시간대"]

    for slot_label in default_slot_labels:
        policy = resolve_policy_for_slot(session, settings.patient_id, slot_label, today)
        views.append(
            {
                "slot_label": slot_label,
                "extra_reminders": policy.extra_reminders,
                "interval_minutes": policy.interval_minutes,
                "missed_dose_after_minutes": policy.missed_dose_after_minutes,
                "primary_reminder_label": policy_primary_reminder_label(
                    policy.primary_reminder_timing,
                    policy.primary_reminder_offset_minutes,
                ),
                "reason": f"기본 정책: 복용 예정 후 {policy.missed_dose_after_minutes}분까지 확인되지 않으면 미복용 AI 알림을 요청합니다.",
                "source_label": "기본 정책",
                "period_label": "현재 날짜 기준",
            }
        )

    return views

def get_dose_status_map(session: Session, notifications: list[Notification]) -> dict[int, str]:
    related_ids = [row.related_dose_event_id for row in notifications if row.related_dose_event_id]
    if not related_ids:
        return {}
    rows = session.scalars(select(DoseEvent).where(DoseEvent.id.in_(related_ids))).all()
    return {row.id: row.status for row in rows}


def visible_chat_prompt_notifications(session: Session, current_time: datetime) -> list[Notification]:
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "conversation_alert",
            Notification.visible_at <= current_time,
            Notification.acknowledged.is_(False),
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(20)
    ).all()
    prompts = []
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        if metadata.get("status") != "agent_ready":
            continue
        if metadata.get("category") not in CHAT_PROMPT_CATEGORIES:
            continue
        prompts.append(row)
    return prompts


def active_chat_prompt_view(session: Session, current_time: datetime) -> dict | None:
    prompts = [
        prompt
        for prompt in visible_chat_prompt_notifications(session, current_time)
        if parse_metadata_json(prompt.metadata_json).get("category") == "policy_confirmation"
    ]
    if not prompts:
        return None
    prompt = prompts[0]
    metadata = parse_metadata_json(prompt.metadata_json)
    return {
        "notification_id": prompt.id,
        "category": metadata.get("category", ""),
        "related_dose_event_id": prompt.related_dose_event_id,
        "label": "정책 선택 답변" if metadata.get("category") == "policy_confirmation" else "미복용 알림 답변",
        "placeholder": (
            "예: 늘리기 / 줄이기 / 현행 유지하기"
            if metadata.get("category") == "policy_confirmation"
            else "예: 깜빡했어요 / 몸이 불편했어요 / 지금 복용했어요"
        ),
    }


def active_ae_prompt_view(session: Session) -> dict | None:
    rows = session.scalars(
        select(ChatMessage)
        .where(ChatMessage.category.in_(["ae_pro_ctcae", "chat", "reply", "multiturn_chat", "missed_dose"]))
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
        .limit(30)
    ).all()
    for row in rows:
        metadata = parse_metadata_json(getattr(row, "metadata_json", "{}"))
        payload = metadata.get("ae_pro_ctcae")
        if not isinstance(payload, dict):
            continue
        questions = payload.get("questions") if isinstance(payload.get("questions"), list) else []
        responses = payload.get("responses") if isinstance(payload.get("responses"), list) else []
        if questions and len(responses) < len(questions):
            return {
                "chat_message_id": row.id,
                "label": "PRO-CTCAE 문항 응답",
                "placeholder": "문항 번호와 답변을 입력해도 됩니다. 예: 1번 보통이다",
            }
    return None

def serialize_notifications(session: Session, notifications: list[Notification]) -> list[dict]:
    dose_status_map = get_dose_status_map(session, notifications)
    serialized = []
    for row in notifications:
        metadata = parse_metadata_json(row.metadata_json)
        serialized.append(
            {
                "id": row.id,
                "notification_type": row.notification_type,
                "title": row.title,
                "body": row.body,
                "visible_at": row.visible_at.isoformat(),
                "visible_at_label": row.visible_at.strftime("%m-%d %H:%M"),
                "acknowledged": row.acknowledged,
                "related_dose_event_id": row.related_dose_event_id,
                "dose_status": dose_status_map.get(row.related_dose_event_id) if row.related_dose_event_id else None,
                "metadata": metadata,
                "agent_job_id": metadata.get("agent_job_id"),
            }
        )
    return serialized

def serialize_notification_feed(
    session: Session,
    current_time: datetime,
    after_id: int = 0,
    limit: int = 10,
) -> list[dict]:
    stmt = (
        select(Notification)
        .where(
            Notification.visible_at <= current_time,
            Notification.id > after_id,
            Notification.acknowledged.is_(False),
            Notification.notification_type != "system_policy_request",
        )
        .order_by(Notification.id.asc())
        .limit(limit)
    )
    rows = [
        row
        for row in session.scalars(stmt).all()
        if is_patient_visible_notification_metadata(parse_metadata_json(row.metadata_json))
    ]
    return serialize_notifications(session, rows)

def get_system_request_history(session: Session, current_time: datetime) -> list[dict]:
    day_start = datetime.combine(current_time.date(), datetime.min.time())
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "system_policy_request",
            Notification.visible_at >= day_start,
            Notification.visible_at <= current_time,
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(20)
    ).all()

    history = []
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        status = metadata.get("status") or "sent"
        status_labels = ["전송됨"]
        if status == "applied":
            status_labels.append("반영됨")
        elif status == "answered":
            status_labels.append("답변됨")
        elif status == "needs_confirmation":
            status_labels.append("확인 대기")
        elif status == "needs_clarification":
            status_labels.append("확인 필요")
        elif status == "failed":
            status_labels.append("실패")
        history.append(
            {
                "id": row.id,
                "message": metadata.get("request_message") or row.body,
                "result_message": metadata.get("result_message", ""),
                "status": status,
                "status_labels": status_labels,
                "time_label": row.visible_at.strftime("%m-%d %H:%M"),
            }
        )
    return history


def daily_pattern_job_status_view(session: Session) -> dict | None:
    job = session.scalar(
        select(AgentJob)
        .where(
            AgentJob.job_type == "daily_pattern",
            AgentJob.status.in_(["pending", "running", "failed"]),
        )
        .order_by(desc(AgentJob.created_at), desc(AgentJob.id))
    )
    if job is None:
        return None
    if job.status == "failed":
        return {
            "status": job.status,
            "status_label": "실패",
            "message": job.error_message or "일일 패턴 처리에 실패했습니다. 오류 알림에서 다시 시도할 수 있습니다.",
        }
    return {
        "status": job.status,
        "status_label": "처리 중" if job.status == "running" else "대기 중",
        "message": "일일 패턴 대화 요청 대기 중: agent 응답을 기다리고 있습니다.",
    }


def build_dashboard_context(request: Request, session: Session, agent_model_config: dict | None = None) -> dict:
    clock = ensure_clock(session)
    timeline_date_param = request.query_params.get("timeline_date")
    try:
        selected_timeline_date = date.fromisoformat(timeline_date_param) if timeline_date_param else clock.current_time.date()
    except ValueError:
        selected_timeline_date = clock.current_time.date()
        timeline_date_param = None
    dose_events = get_dose_events_for_date(session, selected_timeline_date)
    notifications = get_notification_history(session, clock.current_time)
    nutrition = nutrition_dashboard_view(session)
    dose_status_map = get_dose_status_map(session, list(notifications))
    notification_metadata_map = {notification.id: parse_metadata_json(notification.metadata_json) for notification in notifications}
    return {
        "request": request,
        "clock": clock,
        "schedule_preset_options": SCHEDULE_PRESET_OPTIONS,
        "medication_form_options": MEDICATION_FORM_OPTIONS,
        "dosage_form_options": DOSAGE_FORM_OPTIONS,
        "custom_choice": CUSTOM_CHOICE,
        "default_medication_start_date": clock.current_time.date(),
        "default_medication_end_date": clock.current_time.date() + timedelta(days=7),
        "phr_profile": phr_profile_view(session),
        "simulation_readiness": simulation_readiness(session),
        "plans": [medication_plan_view(plan) for plan in list_medication_plans(session)],
        "schedule_map": get_schedule_map(session),
        "dose_events": dose_events,
        "selected_timeline_date": selected_timeline_date,
        "timeline_date_param": timeline_date_param,
        "timeline_calendar": dose_calendar_month_view(session, selected_timeline_date, clock.current_time.date()),
        "notifications": notifications,
        "notification_metadata_map": notification_metadata_map,
        "dose_status_map": dose_status_map,
        "chat_messages": agent_conversation_views(session, clock.current_time),
        "conversation_history_messages": conversation_history_views(session, clock.current_time),
        "active_chat_prompt": active_chat_prompt_view(session, clock.current_time),
        "active_ae_prompt": active_ae_prompt_view(session),
        "active_policies": get_active_policies(session, clock.current_time.date()),
        "nutrition": nutrition,
        "system_policies": [daily_pattern_conversation_time_view(session)],
        "reminder_suppressed_after_side_effect": is_reminder_suppressed_after_side_effect(session),
        "daily_pattern_job_status": daily_pattern_job_status_view(session),
        "system_request_history": get_system_request_history(session, clock.current_time),
        "agent_model_config": agent_model_config or fallback_agent_model_config_view(),
    }

async def resolve_agent_model_config(agent_client: AgentClient) -> dict[str, Any]:
    try:
        return (await agent_client.get_model_config()).model_dump(mode="json")
    except Exception:
        return fallback_agent_model_config_view()
