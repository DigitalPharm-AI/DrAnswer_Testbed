from __future__ import annotations

from sqlalchemy.orm import Session

from shared.schemas import AgentResponse
from system_app.models import DoseEvent
from system_app.services.adherence_pattern_service import (
    create_clinician_escalation_stub,
    evaluate_adherence_pattern,
    validate_pattern_message,
)
from system_app.services.tone_policy_service import select_tone_policy


def missed_dose_adherence_pattern_message(
    session: Session,
    related_dose_event_id: int | None,
    message_metadata: dict,
    response: AgentResponse,
) -> str:
    if related_dose_event_id is None:
        raise ValueError("missed_dose_event_context_missing")
    event = session.get(DoseEvent, related_dose_event_id)
    if event is None:
        raise ValueError("missed_dose_event_context_missing")

    decision = evaluate_adherence_pattern(session, event)
    tone_decision = select_tone_policy(session, event, decision.pattern_code)
    generated_message = str(response.human_summary or "").strip()
    is_valid, errors = validate_pattern_message(
        generated_message,
        medication_name=event.medication_name,
    )
    if not is_valid:
        raise ValueError(
            "missed_dose_llm_message_invalid:"
            + ",".join(errors)
        )

    adherence_metadata = decision.to_metadata()
    adherence_metadata["message"] = generated_message
    adherence_metadata["message_validation"] = {"passed": is_valid, "errors": errors}
    message_metadata["adherence_pattern"] = adherence_metadata

    tone_metadata = tone_decision.to_metadata()
    # The Backend tone catalog is policy evidence only. Its stock message must
    # never be persisted as a candidate that could replace the LLM output.
    tone_metadata.pop("message", None)
    tone_metadata["message_validation"] = {"passed": is_valid, "errors": errors}
    tone_metadata["delivered_message_source"] = "llm_generated"
    message_metadata["tone_policy"] = tone_metadata
    message_metadata["llm_personalization"] = {
        "enabled": True,
        "adjudication_source": "backend_policy",
        "rule_pattern_code": decision.pattern_code,
        "final_pattern_code": decision.pattern_code,
        "message_source": "llm_generated",
        "llm_message_candidate": generated_message,
        "llm_message_validation": {
            "passed": is_valid,
            "errors": errors,
        },
    }

    if decision.escalation_stub_required:
        create_clinician_escalation_stub(session, event, decision)
    return generated_message
