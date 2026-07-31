from __future__ import annotations

from sqlalchemy.orm import Session

from shared.tool_names import PROPOSE_NOTIFICATION_POLICY, PROPOSE_SYSTEM_POLICY
from shared.schemas import AgentResponse, NotificationPolicyDelta, SystemPolicyDelta
from system_app.services.clock_service import pause_simulation_clock_for_conversation
from system_app.services.notification_service import create_notification
from system_app.services.policy_confirmation_constants import (
    POLICY_ACTION_INCREASE,
    POLICY_ACTION_KEEP,
)
from system_app.services.policy_confirmation_diff import (
    actionable_policy_deltas,
)
from system_app.services.policy_confirmation_payload import (
    policy_change_payload,
)
from system_app.services.policy_confirmation_prompt import (
    policy_confirmation_prompt,
)
from system_app.services.policy_service import (
    daily_pattern_conversation_time_view,
    validate_policy_delta,
    validate_system_policy_delta,
)
from system_app.services.timeline_service import add_chat_message

__all__ = [
    "create_policy_confirmation_alert",
    "create_system_policy_confirmation_alert",
    "has_notification_policy_tool_call",
    "has_system_policy_tool_call",
    "policy_deltas_from_tool_response",
    "system_policy_deltas_from_tool_response",
]


def policy_deltas_from_tool_response(response: AgentResponse, source_event_type: str = "") -> list[NotificationPolicyDelta]:
    raw_tool_calls = response.structured_payload.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        raw_tool_call = response.structured_payload.get("tool_call")
        raw_tool_calls = [raw_tool_call] if isinstance(raw_tool_call, dict) else []
    deltas = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        if raw_tool_call.get("name") != PROPOSE_NOTIFICATION_POLICY:
            continue
        arguments = raw_tool_call.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("도구 호출 인자가 비어 있습니다.")
        for payload in _notification_policy_argument_items(arguments):
            normalized = dict(payload)
            normalized["source"] = _normalize_notification_policy_source(normalized.get("source"), source_event_type)
            deltas.append(NotificationPolicyDelta.model_validate(normalized))
    return deltas


def has_notification_policy_tool_call(response: AgentResponse) -> bool:
    raw_tool_calls = response.structured_payload.get("tool_calls")
    if isinstance(raw_tool_calls, list) and any(isinstance(item, dict) and item.get("name") == PROPOSE_NOTIFICATION_POLICY for item in raw_tool_calls):
        return True
    raw_tool_call = response.structured_payload.get("tool_call")
    return isinstance(raw_tool_call, dict) and raw_tool_call.get("name") == PROPOSE_NOTIFICATION_POLICY


def system_policy_deltas_from_tool_response(response: AgentResponse, source_event_type: str = "") -> list[SystemPolicyDelta]:
    raw_tool_calls = response.structured_payload.get("tool_calls")
    if not isinstance(raw_tool_calls, list):
        raw_tool_call = response.structured_payload.get("tool_call")
        raw_tool_calls = [raw_tool_call] if isinstance(raw_tool_call, dict) else []
    deltas = []
    for raw_tool_call in raw_tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue
        if raw_tool_call.get("name") != PROPOSE_SYSTEM_POLICY:
            continue
        arguments = raw_tool_call.get("arguments")
        if not isinstance(arguments, dict):
            raise ValueError("시스템 정책 도구 호출 인자가 비어 있습니다.")
        for payload in _system_policy_argument_items(arguments):
            normalized = dict(payload)
            normalized["policy_key"] = str(normalized.get("policy_key") or "")
            normalized["source"] = _normalize_system_policy_source(normalized.get("source"), source_event_type)
            deltas.append(SystemPolicyDelta.model_validate(normalized))
    return deltas


def has_system_policy_tool_call(response: AgentResponse) -> bool:
    raw_tool_calls = response.structured_payload.get("tool_calls")
    if isinstance(raw_tool_calls, list) and any(isinstance(item, dict) and item.get("name") == PROPOSE_SYSTEM_POLICY for item in raw_tool_calls):
        return True
    raw_tool_call = response.structured_payload.get("tool_call")
    return isinstance(raw_tool_call, dict) and raw_tool_call.get("name") == PROPOSE_SYSTEM_POLICY


def _notification_policy_argument_items(arguments: dict) -> list[dict]:
    raw_items = arguments.get("policies")
    if raw_items is None:
        raw_items = arguments.get("policy_deltas")
    if raw_items is None and isinstance(arguments.get("policy_delta"), dict):
        raw_items = [arguments["policy_delta"]]
    if raw_items is None:
        raw_items = [arguments]
    if not isinstance(raw_items, list):
        raise ValueError(f"{PROPOSE_NOTIFICATION_POLICY} requires a policy object or policies list.")
    return [item for item in raw_items if isinstance(item, dict)]


def _system_policy_argument_items(arguments: dict) -> list[dict]:
    raw_items = arguments.get("policies")
    if raw_items is None:
        raw_items = arguments.get("system_policy_deltas")
    if raw_items is None and isinstance(arguments.get("system_policy_delta"), dict):
        raw_items = [arguments["system_policy_delta"]]
    if raw_items is None:
        raw_items = [arguments]
    if not isinstance(raw_items, list):
        raise ValueError(f"{PROPOSE_SYSTEM_POLICY} requires a policy object or policies list.")
    return [item for item in raw_items if isinstance(item, dict)]


def _normalize_notification_policy_source(value: object, source_event_type: str) -> str:
    if source_event_type in {"daily_pattern", "manual_daily_pattern"}:
        return "pattern_analysis"
    if source_event_type == "multiturn_chat":
        return "patient_request"
    allowed = {"pattern_analysis", "patient_request", "system_request"}
    if isinstance(value, str) and value in allowed:
        return value
    return "system_request"


def _normalize_system_policy_source(value: object, source_event_type: str) -> str:
    allowed = {"patient_request", "system_request"}
    if isinstance(value, str) and value in allowed:
        return value
    if source_event_type == "multiturn_chat":
        return "patient_request"
    return "system_request"


def _system_policy_multiple_choice(delta: SystemPolicyDelta) -> dict:
    question = f"{_system_policy_label(delta.policy_key)}을 {delta.value}로 변경하시는 건 어떨까요?"
    return {
        "prompt_type": "policy_confirmation",
        "question": question,
        "recommended_value": POLICY_ACTION_INCREASE,
        "options": [
            {
                "number": 1,
                "value": POLICY_ACTION_INCREASE,
                "label": "적용하기",
                "text": "1. 적용하기",
                "description": "제안된 시스템 정책 변경을 적용합니다.",
            },
            {
                "number": 2,
                "value": POLICY_ACTION_KEEP,
                "label": "현행 유지",
                "text": "2. 현행 유지",
                "description": "현재 시스템 정책을 유지합니다.",
            },
        ],
    }


def _system_policy_label(policy_key: str) -> str:
    if policy_key == "daily_pattern_conversation_time":
        return "일일 패턴 대화 요청 시간"
    return policy_key


def _system_policy_value_display(value: str, *, source_label: str = "") -> dict:
    return {
        "value": value,
        "source_label": source_label,
        "summary": value,
    }


def _system_policy_candidate(session: Session, delta: SystemPolicyDelta) -> dict:
    current = daily_pattern_conversation_time_view(session)
    current_value = str(current.get("value") or "")
    return {
        "slot_label": _system_policy_label(delta.policy_key),
        "current": _system_policy_value_display(current_value, source_label=str(current.get("source_label") or "")),
        "proposed": _system_policy_value_display(delta.value, source_label="변경 후보"),
        "changed_fields": ["value"] if current_value != delta.value else [],
        "reason": delta.reason,
        "raw_delta": delta.model_dump(mode="json"),
        "summary": f"{_system_policy_label(delta.policy_key)}: {current_value} -> {delta.value}",
    }


def _system_policy_change_payload(session: Session, deltas: list[SystemPolicyDelta], multiple_choice: dict) -> dict:
    return {
        "type": "system_policy",
        "question": str(multiple_choice.get("question") or "시스템 정책을 변경하시는 건 어떨까요?"),
        "candidates": [_system_policy_candidate(session, delta) for delta in deltas],
        "options": multiple_choice.get("options", []),
        "recommended_action": multiple_choice.get("recommended_value"),
    }


def create_policy_confirmation_alert(
    session: Session,
    response: AgentResponse,
    source_event_type: str,
    deltas: list[NotificationPolicyDelta],
) -> tuple[bool, str]:
    if not deltas:
        return False, "정책 도구 호출 항목이 없습니다."
    for delta in deltas:
        is_valid, message = validate_policy_delta(delta, session=session)
        if not is_valid:
            return False, message

    deltas = actionable_policy_deltas(session, deltas)
    if not deltas:
        return False, "현재 정책과 같은 제안이라 확인 알림을 만들지 않았습니다."

    clock, resume_state = pause_simulation_clock_for_conversation(session)
    multiple_choice = policy_confirmation_prompt(session, deltas, response.human_summary)
    policy_change = policy_change_payload(session, deltas, multiple_choice)
    confirmation_options = [
        str(option.get("text"))
        for option in multiple_choice.get("options", [])
        if isinstance(option, dict) and option.get("text")
    ]
    notification = create_notification(
        session,
        notification_type="conversation_alert",
        title="AI가 대화를 요청합니다.",
        body=policy_change["question"],
        visible_at=clock.current_time,
        metadata={
            "category": "policy_confirmation",
            "status": "agent_ready",
            "proposed_policies": [delta.model_dump(mode="json") for delta in deltas],
            "policy_change": policy_change,
            "multiple_choice": multiple_choice,
            "confirmation_options": confirmation_options,
            "recommended_action": multiple_choice.get("recommended_value"),
            "resume_clock": resume_state,
        },
    )
    add_chat_message(
        session,
        role="assistant",
        content=policy_change["question"],
        sender_type="assistant",
        category="policy_confirmation",
        metadata={
            "conversation_alert": {
                "notification_id": notification.id,
                "category": "policy_confirmation",
                "status": "agent_ready",
                "reply_mode": "chat",
            },
            "policy_confirmation": {
                "notification_id": notification.id,
                "policy_change": policy_change,
                "multiple_choice": multiple_choice,
                "recommended_action": multiple_choice.get("recommended_value"),
            },
        },
    )
    return False, "정책 변경 후보를 채팅에 표시했습니다."


def create_system_policy_confirmation_alert(
    session: Session,
    response: AgentResponse,
    source_event_type: str,
    deltas: list[SystemPolicyDelta],
) -> tuple[bool, str]:
    if not deltas:
        return False, "시스템 정책 도구 호출 항목이 없습니다."
    for delta in deltas:
        is_valid, message = validate_system_policy_delta(delta)
        if not is_valid:
            return False, message

    actionable_deltas = []
    for delta in deltas:
        current = daily_pattern_conversation_time_view(session)
        if str(current.get("value") or "") != delta.value:
            actionable_deltas.append(delta)
    if not actionable_deltas:
        return False, "현재 시스템 정책과 같은 제안이라 확인 알림을 만들지 않았습니다."

    clock, resume_state = pause_simulation_clock_for_conversation(session)
    multiple_choice = _system_policy_multiple_choice(actionable_deltas[0])
    policy_change = _system_policy_change_payload(session, actionable_deltas, multiple_choice)
    confirmation_options = [
        str(option.get("text"))
        for option in multiple_choice.get("options", [])
        if isinstance(option, dict) and option.get("text")
    ]
    notification = create_notification(
        session,
        notification_type="conversation_alert",
        title="AI가 대화를 요청합니다.",
        body=policy_change["question"],
        visible_at=clock.current_time,
        metadata={
            "category": "policy_confirmation",
            "status": "agent_ready",
            "proposed_system_policies": [delta.model_dump(mode="json") for delta in actionable_deltas],
            "policy_change": policy_change,
            "multiple_choice": multiple_choice,
            "confirmation_options": confirmation_options,
            "recommended_action": multiple_choice.get("recommended_value"),
            "resume_clock": resume_state,
        },
    )
    add_chat_message(
        session,
        role="assistant",
        content=policy_change["question"],
        sender_type="assistant",
        category="policy_confirmation",
        metadata={
            "conversation_alert": {
                "notification_id": notification.id,
                "category": "policy_confirmation",
                "status": "agent_ready",
                "reply_mode": "chat",
            },
            "policy_confirmation": {
                "notification_id": notification.id,
                "policy_change": policy_change,
                "multiple_choice": multiple_choice,
                "recommended_action": multiple_choice.get("recommended_value"),
            },
        },
    )
    return False, "정책 변경 후보를 채팅에 표시했습니다."
