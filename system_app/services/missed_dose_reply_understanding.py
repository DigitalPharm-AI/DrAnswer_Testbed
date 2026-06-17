from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.schemas import AgentResponse
from system_app.models import ChatMessage, Notification

UNDERSTANDING_METADATA_KEY = "missed_dose_reply_understanding"
MISSED_DOSE_REPLY_METADATA_KEY = "missed_dose_reply"

ALLOWED_REPLY_INTENTS = {
    "missed_reason",
    "taken_confirmation",
    "snooze_request",
    "suppression_request",
    "general_reply",
    "unknown",
}
ALLOWED_BARRIER_TYPES = {
    "forgetfulness",
    "side_effect_concern",
    "busy",
    "low_motivation",
    "notification_burden",
    "already_taken",
    "delay_intent",
    "unknown",
}
ALLOWED_REACTION_ACTIONS = {"reply", "take", "snooze", "suppress_candidate", "ignore"}
ALLOWED_TONES = {"empathy", "persuasion", "practical", "warning_soft", "side_effect_check"}

TAKEN_KEYWORDS = ("먹었", "복용했", "복용 완료", "먹음", "챙겼", "먹었다", "먹었어", "기록해")
NEGATED_TAKEN_KEYWORDS = ("못 먹", "못먹", "안 먹", "안먹", "복용 못", "복용 안")
SIDE_EFFECT_KEYWORDS = ("메스꺼", "구역", "구토", "속이", "설사", "두통", "어지러", "불편", "부작용", "약 때문")
NOTIFICATION_BURDEN_KEYWORDS = ("부담", "짜증", "스트레스", "알림이 너무", "계속 알림")
SUPPRESS_KEYWORDS = ("알림 끄", "알림 꺼", "그만 보내", "보내지 마", "안 받고", "끄고 싶", "꺼줘")
FORGETFULNESS_KEYWORDS = ("깜빡", "까먹", "잊었", "잊어")
BUSY_KEYWORDS = ("바빠", "바빴", "회의", "출근", "운전", "정신없")
DELAY_KEYWORDS = ("나중", "이따", "좀 있다", "잠시 후", "10분", "30분", "미루")
LOW_MOTIVATION_KEYWORDS = ("하기 싫", "먹기 싫", "귀찮", "그냥 안", "내키지")


def build_rule_based_missed_dose_reply_understanding(message: str) -> dict[str, Any]:
    text = message.strip()
    lower_text = text.lower()
    if not text:
        return _understanding(
            reply_intent="unknown",
            barrier_type="unknown",
            reaction_action="ignore",
            confidence=0.2,
            evidence=["empty_reply"],
            source="system_rule",
        )

    if _contains_any(lower_text, TAKEN_KEYWORDS) and not _contains_any(lower_text, NEGATED_TAKEN_KEYWORDS):
        return _understanding(
            reply_intent="taken_confirmation",
            barrier_type="already_taken",
            reaction_action="take",
            confidence=0.86,
            evidence=["taken_confirmation_keyword"],
            source="system_rule",
            prefer_tone="practical",
        )

    if _contains_any(lower_text, SIDE_EFFECT_KEYWORDS):
        return _understanding(
            reply_intent="missed_reason",
            barrier_type="side_effect_concern",
            reaction_action="reply",
            confidence=0.84,
            evidence=["side_effect_keyword"],
            source="system_rule",
            prefer_tone="side_effect_check",
            avoid_tones=["warning_soft"],
            needs_side_effect_check=True,
            needs_support_first=True,
        )

    if _contains_any(lower_text, SUPPRESS_KEYWORDS):
        return _understanding(
            reply_intent="suppression_request",
            barrier_type="notification_burden",
            reaction_action="suppress_candidate",
            confidence=0.82,
            evidence=["suppression_keyword"],
            source="system_rule",
            avoid_tones=["warning_soft"],
            needs_support_first=True,
            suppress_candidate=True,
        )

    if _contains_any(lower_text, NOTIFICATION_BURDEN_KEYWORDS):
        return _understanding(
            reply_intent="missed_reason",
            barrier_type="notification_burden",
            reaction_action="reply",
            confidence=0.74,
            evidence=["notification_burden_keyword"],
            source="system_rule",
            prefer_tone="empathy",
            avoid_tones=["warning_soft"],
            needs_support_first=True,
        )

    if _contains_any(lower_text, FORGETFULNESS_KEYWORDS):
        return _understanding(
            reply_intent="missed_reason",
            barrier_type="forgetfulness",
            reaction_action="reply",
            confidence=0.78,
            evidence=["forgetfulness_keyword"],
            source="system_rule",
            prefer_tone="practical",
        )

    if _contains_any(lower_text, BUSY_KEYWORDS):
        return _understanding(
            reply_intent="missed_reason",
            barrier_type="busy",
            reaction_action="reply",
            confidence=0.76,
            evidence=["busy_keyword"],
            source="system_rule",
            prefer_tone="practical",
        )

    if _contains_any(lower_text, DELAY_KEYWORDS):
        return _understanding(
            reply_intent="snooze_request",
            barrier_type="delay_intent",
            reaction_action="snooze",
            confidence=0.72,
            evidence=["delay_keyword"],
            source="system_rule",
            prefer_tone="practical",
        )

    if _contains_any(lower_text, LOW_MOTIVATION_KEYWORDS):
        return _understanding(
            reply_intent="missed_reason",
            barrier_type="low_motivation",
            reaction_action="reply",
            confidence=0.68,
            evidence=["low_motivation_keyword"],
            source="system_rule",
            prefer_tone="practical",
            needs_support_first=True,
        )

    return _understanding(
        reply_intent="general_reply",
        barrier_type="unknown",
        reaction_action="reply",
        confidence=0.45,
        evidence=["no_specific_keyword"],
        source="system_rule",
    )


def annotate_missed_dose_reply(
    session: Session,
    notification: Notification,
    *,
    patient_reply: str,
    understanding: dict[str, Any],
    prompt_message: ChatMessage | None = None,
) -> dict[str, Any]:
    normalized = normalize_missed_dose_reply_understanding(understanding)
    metadata = parse_json_object(notification.metadata_json)
    metadata["status"] = "reply_submitted"
    metadata["patient_reply"] = patient_reply
    metadata[UNDERSTANDING_METADATA_KEY] = normalized
    notification.metadata_json = dump_json(metadata)

    if prompt_message is not None:
        message_metadata = parse_json_object(prompt_message.metadata_json)
        message_metadata["patient_reply"] = patient_reply
        message_metadata[UNDERSTANDING_METADATA_KEY] = normalized
        prompt_message.metadata_json = dump_json(message_metadata)

    session.flush()
    return normalized


def missed_dose_reply_request_metadata(notification: Notification, understanding: dict[str, Any]) -> dict[str, Any]:
    return {
        MISSED_DOSE_REPLY_METADATA_KEY: {
            "conversation_alert_id": notification.id,
            "related_dose_event_id": notification.related_dose_event_id,
            "understanding": normalize_missed_dose_reply_understanding(understanding),
        }
    }


def merge_missed_dose_reply_understanding_from_agent_response(
    session: Session,
    request_notification_id: int,
    response: AgentResponse,
) -> dict[str, Any] | None:
    request_notification = session.get(Notification, request_notification_id)
    if request_notification is None:
        return None
    request_metadata = parse_json_object(request_notification.metadata_json)
    reply_context = request_metadata.get(MISSED_DOSE_REPLY_METADATA_KEY)
    if not isinstance(reply_context, dict):
        return None
    current = reply_context.get("understanding") if isinstance(reply_context.get("understanding"), dict) else {}
    agent_understanding = _agent_understanding_payload(response.structured_payload)
    if not agent_understanding:
        return None
    merged = merge_missed_dose_reply_understanding(current, agent_understanding)
    reply_context["understanding"] = merged
    request_metadata[MISSED_DOSE_REPLY_METADATA_KEY] = reply_context
    request_notification.metadata_json = dump_json(request_metadata)

    alert_id = _safe_int(reply_context.get("conversation_alert_id"))
    if alert_id is not None:
        alert = session.get(Notification, alert_id)
        if alert is not None:
            alert_metadata = parse_json_object(alert.metadata_json)
            alert_metadata[UNDERSTANDING_METADATA_KEY] = merged
            alert.metadata_json = dump_json(alert_metadata)

    user_message_id = _safe_int(request_metadata.get("chat_message_id"))
    if user_message_id is not None:
        user_message = session.get(ChatMessage, user_message_id)
        if user_message is not None:
            message_metadata = parse_json_object(user_message.metadata_json)
            message_metadata[MISSED_DOSE_REPLY_METADATA_KEY] = reply_context
            user_message.metadata_json = dump_json(message_metadata)

    session.flush()
    return merged


def merge_missed_dose_reply_understanding(current: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    base = normalize_missed_dose_reply_understanding(current)
    incoming = normalize_missed_dose_reply_understanding(candidate, source="agent_structured_payload")
    if incoming["confidence"] < base["confidence"] and incoming["reply_intent"] == "unknown":
        return base
    merged = deepcopy(base)
    for key in ("reply_intent", "barrier_type", "reaction_action", "confidence", "evidence", "source"):
        merged[key] = incoming[key]
    merged["policy_signals"] = {
        **base.get("policy_signals", {}),
        **incoming.get("policy_signals", {}),
    }
    merged["fallback_understanding"] = base
    return normalize_missed_dose_reply_understanding(merged)


def normalize_missed_dose_reply_understanding(value: dict[str, Any], *, source: str | None = None) -> dict[str, Any]:
    policy_signals = value.get("policy_signals") if isinstance(value.get("policy_signals"), dict) else {}
    avoid_tones = policy_signals.get("avoid_tones")
    if isinstance(avoid_tones, str):
        avoid_tone_values = [avoid_tones]
    elif isinstance(avoid_tones, list):
        avoid_tone_values = [str(item) for item in avoid_tones]
    else:
        avoid_tone_values = []
    normalized = {
        "reply_intent": _allowed(str(value.get("reply_intent") or "unknown"), ALLOWED_REPLY_INTENTS, "unknown"),
        "barrier_type": _allowed(str(value.get("barrier_type") or "unknown"), ALLOWED_BARRIER_TYPES, "unknown"),
        "reaction_action": _allowed(str(value.get("reaction_action") or "reply"), ALLOWED_REACTION_ACTIONS, "reply"),
        "confidence": _confidence(value.get("confidence")),
        "evidence": _string_list(value.get("evidence")),
        "source": source or str(value.get("source") or "system_rule"),
        "policy_signals": {
            "prefer_tone": _allowed(str(policy_signals.get("prefer_tone") or ""), ALLOWED_TONES, ""),
            "avoid_tones": [_allowed(tone, ALLOWED_TONES, "") for tone in avoid_tone_values if _allowed(tone, ALLOWED_TONES, "")],
            "needs_side_effect_check": bool(policy_signals.get("needs_side_effect_check")),
            "needs_support_first": bool(policy_signals.get("needs_support_first")),
            "suppress_candidate": bool(policy_signals.get("suppress_candidate")),
        },
    }
    if isinstance(value.get("fallback_understanding"), dict):
        normalized["fallback_understanding"] = normalize_missed_dose_reply_understanding(value["fallback_understanding"])
    return normalized


def _understanding(
    *,
    reply_intent: str,
    barrier_type: str,
    reaction_action: str,
    confidence: float,
    evidence: list[str],
    source: str,
    prefer_tone: str = "",
    avoid_tones: list[str] | None = None,
    needs_side_effect_check: bool = False,
    needs_support_first: bool = False,
    suppress_candidate: bool = False,
) -> dict[str, Any]:
    return normalize_missed_dose_reply_understanding(
        {
            "reply_intent": reply_intent,
            "barrier_type": barrier_type,
            "reaction_action": reaction_action,
            "confidence": confidence,
            "evidence": evidence,
            "source": source,
            "policy_signals": {
                "prefer_tone": prefer_tone,
                "avoid_tones": avoid_tones or [],
                "needs_side_effect_check": needs_side_effect_check,
                "needs_support_first": needs_support_first,
                "suppress_candidate": suppress_candidate,
            },
        }
    )


def _agent_understanding_payload(structured_payload: dict[str, Any]) -> dict[str, Any]:
    direct = structured_payload.get(UNDERSTANDING_METADATA_KEY)
    if isinstance(direct, dict):
        return direct
    model_output = structured_payload.get("model_output")
    if isinstance(model_output, dict):
        nested = model_output.get(UNDERSTANDING_METADATA_KEY)
        if isinstance(nested, dict):
            return nested
    return {}


def _contains_any(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _allowed(value: str, allowed: set[str], fallback: str) -> str:
    return value if value in allowed else fallback


def _confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
