from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import AgentResponse, NotificationPolicyDelta
from system_app.services.audit_service import audit_response_for_policy_delta, record_agent_audit
from system_app.services.policy_service import apply_policy_delta, validate_policy_delta


def policy_apply_result_message(delta: NotificationPolicyDelta, message: str) -> str:
    return f"{delta.slot_label}: {message}"


def apply_policy_deltas_individually(
    session: Session,
    response: AgentResponse,
    source_event_type: str,
    deltas: list[NotificationPolicyDelta],
    *,
    audit_payload_for_delta,
) -> tuple[bool, str]:
    for delta in deltas:
        is_valid, message = validate_policy_delta(delta, session=session)
        if not is_valid:
            raise ValueError(message)

    messages: list[str] = []
    for delta in deltas:
        applied, message = apply_policy_delta(session, delta)
        audit_response = audit_response_for_policy_delta(
            response,
            delta,
            structured_payload=audit_payload_for_delta(delta),
        )
        if not applied:
            record_agent_audit(session, audit_response, source_event_type, applied=False, error_message=message)
            raise ValueError(message)
        record_agent_audit(session, audit_response, source_event_type, applied=True, error_message="")
        messages.append(policy_apply_result_message(delta, message))
    return True, " / ".join(messages)
