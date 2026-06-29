from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json
from shared.redaction import redact_for_logging, redact_inline_secrets, redacted_clinical_text_label
from shared.schemas import AgentResponse, NotificationPolicyDelta
from system_app.models import AgentDecisionAudit


def create_agent_decision_audit(
    session: Session,
    *,
    trace_id: str,
    agent_name: str,
    prompt_version_id: str,
    decision_type: str,
    structured_payload: dict | None,
    human_summary: str = "",
    applied: bool = False,
    error_message: str = "",
    source_event_type: str,
    flush: bool = False,
) -> AgentDecisionAudit:
    audit = AgentDecisionAudit(
        trace_id=trace_id,
        agent_name=agent_name,
        prompt_version_id=prompt_version_id,
        decision_type=decision_type,
        structured_payload=dump_json(redact_for_logging(structured_payload or {})),
        human_summary=redacted_clinical_text_label(human_summary) if human_summary else "",
        applied=applied,
        error_message=redact_inline_secrets(error_message, limit=400),
        source_event_type=source_event_type,
    )
    session.add(audit)
    if flush:
        session.flush()
    return audit


def record_agent_audit(
    session: Session,
    response: AgentResponse,
    source_event_type: str,
    applied: bool,
    error_message: str = "",
) -> AgentDecisionAudit:
    return create_agent_decision_audit(
        session,
        trace_id=response.trace_id,
        agent_name=response.agent_name,
        prompt_version_id=response.prompt_version_id,
        decision_type=response.decision_type,
        structured_payload=response.structured_payload,
        human_summary=response.human_summary,
        applied=applied,
        error_message=error_message,
        source_event_type=source_event_type,
        flush=True,
    )


def audit_response_for_policy_delta(
    response: AgentResponse,
    delta: NotificationPolicyDelta,
    *,
    structured_payload: dict | None = None,
) -> AgentResponse:
    return response.model_copy(
        update={
            "structured_payload": structured_payload or delta.model_dump(mode="json"),
            "human_summary": f"{delta.slot_label}: {response.human_summary or delta.reason}",
        }
    )
