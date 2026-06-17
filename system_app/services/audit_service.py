from __future__ import annotations

from sqlalchemy.orm import Session

from shared.json_utils import dump_json
from shared.schemas import AgentResponse, NotificationPolicyDelta
from system_app.models import AgentDecisionAudit


def record_agent_audit(
    session: Session,
    response: AgentResponse,
    source_event_type: str,
    applied: bool,
    error_message: str = "",
) -> AgentDecisionAudit:
    audit = AgentDecisionAudit(
        trace_id=response.trace_id,
        agent_name=response.agent_name,
        prompt_version_id=response.prompt_version_id,
        decision_type=response.decision_type,
        structured_payload=dump_json(response.structured_payload),
        human_summary=response.human_summary,
        applied=applied,
        error_message=error_message,
        source_event_type=source_event_type,
    )
    session.add(audit)
    session.flush()
    return audit


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
