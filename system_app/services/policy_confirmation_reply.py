from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from shared.schemas import AgentResponse, NotificationPolicyDelta, SystemPolicyDelta
from shared.time_utils import utc_now
from system_app.models import Notification
from system_app.services.audit_service import record_agent_audit
from system_app.services.policy_confirmation_apply import apply_policy_deltas_individually
from system_app.services.policy_confirmation_constants import POLICY_ACTION_DECREASE, POLICY_ACTION_INCREASE, POLICY_ACTION_KEEP
from system_app.services.policy_confirmation_diff import selected_policy_deltas_for_confirmation
from system_app.services.policy_service import apply_system_policy_delta


def policy_confirmation_action(message: str) -> str | None:
    payload = parse_json_object(message)
    if payload:
        action = payload.get("action") or payload.get("value")
        if isinstance(action, str):
            message = action
    compact = message.replace(" ", "").strip().lower()
    if compact in {"increase", "1"}:
        return POLICY_ACTION_INCREASE
    if compact in {"decrease", "2"}:
        return POLICY_ACTION_DECREASE
    if compact in {"keep", "3"}:
        return POLICY_ACTION_KEEP
    if any(token in compact for token in ("현행유지", "유지", "그대로", "변경안함", "안바꿔", "취소")):
        return POLICY_ACTION_KEEP
    if any(token in compact for token in ("줄이기", "줄여", "감소", "적게", "덜")):
        return POLICY_ACTION_DECREASE
    if any(token in compact for token in ("늘리기", "늘려", "증가", "강화", "적용", "좋아", "확인", "네", "응")):
        return POLICY_ACTION_INCREASE
    return None


def resolve_multiple_choice_reply(message: str, multiple_choice: object) -> str | None:
    if not isinstance(multiple_choice, dict):
        return None
    options = multiple_choice.get("options")
    if not isinstance(options, list):
        return None
    payload = parse_json_object(message)
    if payload:
        action = payload.get("action") or payload.get("value")
        if isinstance(action, str):
            message = action
    normalized = message.strip().lower()
    compact = normalized.replace(" ", "")
    for option in options:
        if not isinstance(option, dict):
            continue
        value = str(option.get("value") or "").strip()
        number = str(option.get("number") or "").strip()
        label = str(option.get("label") or "").strip()
        text = str(option.get("text") or "").strip()
        candidates = {value.lower(), number.lower(), label.lower(), text.lower()}
        if number and label:
            candidates.add(f"{number}. {label}".lower())
            candidates.add(f"{number}.{label}".lower())
            candidates.add(f"{number}{label}".lower())
        if normalized in candidates or compact in {candidate.replace(" ", "") for candidate in candidates}:
            return value
    return None


def policy_confirmation_response(metadata: dict, human_summary: str, structured_payload: dict) -> AgentResponse:
    return AgentResponse(
        trace_id=metadata.get("trace_id") or f"policy-confirmation-{utc_now().timestamp()}",
        agent_name=metadata.get("agent_name") or "policy_confirmation",
        prompt_version_id=metadata.get("prompt_version_id") or "n/a",
        decision_type="policy_confirmation",
        structured_payload=structured_payload,
        human_summary=human_summary,
        requires_conversation_alert=False,
    )


def handle_policy_confirmation_reply(session: Session, notification: Notification, message: str) -> tuple[bool, str]:
    metadata = parse_json_object(notification.metadata_json)
    raw_system_policies = metadata.get("proposed_system_policies") if isinstance(metadata.get("proposed_system_policies"), list) else []
    system_deltas = [SystemPolicyDelta.model_validate(item) for item in raw_system_policies if isinstance(item, dict)]
    if system_deltas:
        return _handle_system_policy_confirmation_reply(session, metadata, message, system_deltas)

    raw_policies = metadata.get("proposed_policies") if isinstance(metadata.get("proposed_policies"), list) else []
    deltas = [NotificationPolicyDelta.model_validate(item) for item in raw_policies if isinstance(item, dict)]
    if not deltas:
        response = policy_confirmation_response(metadata, "확인할 정책 후보가 없어 변경하지 않았습니다.", {"proposed_policies": []})
        record_agent_audit(session, response, "policy_confirmation", applied=False, error_message="정책 후보가 없습니다.")
        return False, "확인할 정책 후보가 없어 변경하지 않았습니다."

    action = resolve_multiple_choice_reply(message, metadata.get("multiple_choice")) or policy_confirmation_action(message)
    if action is None:
        response = policy_confirmation_response(metadata, "정책 변경 선택을 이해하지 못해 현행 정책을 유지했습니다.", {"patient_reply": message})
        record_agent_audit(session, response, "policy_confirmation", applied=False, error_message="unrecognized_policy_confirmation_reply")
        return False, "정책 변경 선택을 이해하지 못했습니다. 현행 정책을 유지했습니다."
    if action == POLICY_ACTION_KEEP:
        response = policy_confirmation_response(metadata, "현행 알림 정책을 유지했습니다.", {"patient_reply": message, "action": action})
        record_agent_audit(session, response, "policy_confirmation", applied=False, error_message="user_kept_current_policy")
        return False, "현행 알림 정책을 유지했습니다."

    recommended_action = metadata.get("recommended_action")
    selected_deltas = selected_policy_deltas_for_confirmation(
        session,
        deltas,
        action,
        recommended_action=recommended_action if isinstance(recommended_action, str) else None,
    )
    response = policy_confirmation_response(
        metadata,
        "정책 확인 답변에 따라 알림 정책을 반영했습니다.",
        {
            "patient_reply": message,
            "action": action,
            "tool_calls": [
                {
                    "name": "apply_notification_policy",
                    "arguments": delta.model_dump(mode="json"),
                }
                for delta in selected_deltas
            ],
        },
    )
    return apply_policy_deltas_individually(
        session,
        response,
        "policy_confirmation",
        selected_deltas,
        audit_payload_for_delta=lambda delta: {
            "patient_reply": message,
            "action": action,
            "tool_call": {
                "name": "apply_notification_policy",
                "arguments": delta.model_dump(mode="json"),
            },
        },
    )


def _handle_system_policy_confirmation_reply(session: Session, metadata: dict, message: str, deltas: list[SystemPolicyDelta]) -> tuple[bool, str]:
    action = resolve_multiple_choice_reply(message, metadata.get("multiple_choice")) or policy_confirmation_action(message)
    if action is None:
        response = policy_confirmation_response(metadata, "시스템 정책 변경 선택을 이해하지 못해 현행 정책을 유지했습니다.", {"patient_reply": message})
        record_agent_audit(session, response, "policy_confirmation", applied=False, error_message="unrecognized_system_policy_confirmation_reply")
        return False, "시스템 정책 변경 선택을 이해하지 못했습니다. 현행 정책을 유지했습니다."
    if action == POLICY_ACTION_KEEP:
        response = policy_confirmation_response(metadata, "현행 시스템 정책을 유지했습니다.", {"patient_reply": message, "action": action})
        record_agent_audit(session, response, "policy_confirmation", applied=False, error_message="user_kept_current_system_policy")
        return False, "현행 시스템 정책을 유지했습니다."

    result_messages = []
    all_applied = True
    for delta in deltas:
        applied, message_text = apply_system_policy_delta(session, delta)
        all_applied = all_applied and applied
        result_messages.append(f"{delta.policy_key}: {message_text}")
        response = policy_confirmation_response(
            metadata,
            message_text,
            {
                "patient_reply": message,
                "action": action,
                "tool_call": {
                    "name": "apply_system_policy",
                    "arguments": delta.model_dump(mode="json"),
                },
            },
        )
        record_agent_audit(session, response, "policy_confirmation", applied=applied, error_message="" if applied else message_text)
    return all_applied, " / ".join(result_messages)
