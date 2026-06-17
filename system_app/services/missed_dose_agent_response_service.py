from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import AgentResponse
from system_app.models import DoseEvent
from system_app.services.adherence_pattern_service import create_clinician_escalation_stub, evaluate_adherence_pattern
from system_app.services.missed_dose_hybrid_service import resolve_message_with_llm_candidate, resolve_pattern_with_llm_candidate
from system_app.services.tone_policy_service import select_tone_policy


def missed_dose_adherence_pattern_message(
    session: Session,
    related_dose_event_id: int | None,
    message_metadata: dict,
    response: AgentResponse,
) -> str:
    if related_dose_event_id is None:
        return ""
    event = session.get(DoseEvent, related_dose_event_id)
    if event is None:
        return ""

    rule_decision = evaluate_adherence_pattern(session, event)
    pattern_resolution = resolve_pattern_with_llm_candidate(rule_decision, response)
    decision = pattern_resolution.decision
    tone_decision = select_tone_policy(session, event, decision.pattern_code)
    message_resolution = resolve_message_with_llm_candidate(response, event, decision, tone_decision)

    is_valid = bool(message_resolution.validation["passed"])
    errors = list(message_resolution.validation["errors"])
    adherence_metadata = decision.to_metadata()
    adherence_metadata["message"] = message_resolution.message
    adherence_metadata["message_validation"] = {"passed": is_valid, "errors": errors}
    message_metadata["adherence_pattern"] = adherence_metadata

    tone_metadata = tone_decision.to_metadata()
    tone_metadata["message_validation"] = {"passed": is_valid, "errors": errors}
    message_metadata["tone_policy"] = tone_metadata
    message_metadata["llm_personalization"] = {
        **pattern_resolution.metadata,
        **message_resolution.metadata,
    }

    if decision.escalation_stub_required:
        create_clinician_escalation_stub(session, event, decision)
    return message_resolution.message if is_valid else ""
