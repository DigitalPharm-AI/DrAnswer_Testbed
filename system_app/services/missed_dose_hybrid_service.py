from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from shared.schemas import AgentResponse
from system_app.models import DoseEvent
from system_app.services.adherence_pattern_service import (
    PATTERN_LABELS,
    PATTERN_MESSAGES,
    PATTERN_TONES,
    AdherencePatternDecision,
    validate_pattern_message,
)
from system_app.services.tone_policy_service import PATTERN_CODES, TonePolicyDecision

LLM_PATTERN_CONFIDENCE_THRESHOLD = 0.65


@dataclass(frozen=True)
class PatternResolution:
    decision: AdherencePatternDecision
    metadata: dict[str, Any]


@dataclass(frozen=True)
class MessageResolution:
    message: str
    metadata: dict[str, Any]
    validation: dict[str, Any]


def resolve_pattern_with_llm_candidate(rule_decision: AdherencePatternDecision, response: AgentResponse) -> PatternResolution:
    llm_candidate = extract_llm_hybrid_candidate(response)
    rule_confidence, adjudication_needed, ambiguity_reason = rule_pattern_confidence(rule_decision)
    llm_pattern_code = _clean_pattern_code(llm_candidate.get("pattern_code"))
    llm_confidence = _float_value(llm_candidate.get("pattern_confidence"))
    llm_reason = str(llm_candidate.get("judgement_reason") or llm_candidate.get("pattern_reason") or "")
    source = "rule"
    final_decision = rule_decision
    fallback_reason = ""

    if adjudication_needed:
        source = "rule_fallback"
        fallback_reason = "llm_candidate_missing_or_low_confidence"
        if llm_pattern_code and llm_confidence >= LLM_PATTERN_CONFIDENCE_THRESHOLD:
            final_decision = _decision_for_code(rule_decision, llm_pattern_code, llm_reason)
            source = "llm_adjudication"
            fallback_reason = ""

    metadata = {
        "enabled": True,
        "rule_pattern_code": rule_decision.pattern_code,
        "final_pattern_code": final_decision.pattern_code,
        "rule_confidence": rule_confidence,
        "adjudication_needed": adjudication_needed,
        "ambiguity_reason": ambiguity_reason,
        "adjudication_source": source,
        "llm_pattern_code": llm_pattern_code,
        "llm_pattern_confidence": llm_confidence,
        "llm_judgement_reason": llm_reason,
        "fallback_reason": fallback_reason,
    }
    return PatternResolution(decision=final_decision, metadata=metadata)


def resolve_message_with_llm_candidate(
    response: AgentResponse,
    event: DoseEvent,
    pattern_decision: AdherencePatternDecision,
    tone_decision: TonePolicyDecision,
) -> MessageResolution:
    llm_candidate = extract_llm_hybrid_candidate(response)
    candidate_reason = str(llm_candidate.get("reason") or "")
    candidate_message = _clean_message(llm_candidate.get("generated_message"))
    candidate_tone = str(llm_candidate.get("tone_key") or "")
    candidate_pattern = _clean_pattern_code(llm_candidate.get("pattern_code"))
    candidate_safety_notes = _string_list(llm_candidate.get("safety_notes"))
    fallback_message = tone_decision.message
    fallback_valid, fallback_errors = validate_pattern_message(fallback_message, medication_name=event.medication_name)
    candidate_valid, candidate_errors = validate_pattern_message(candidate_message, medication_name=event.medication_name)

    message = fallback_message
    source = "csv_fallback"
    fallback_reason = "llm_message_missing"
    if candidate_message:
        fallback_reason = ""
        if candidate_pattern and candidate_pattern != pattern_decision.pattern_code:
            fallback_reason = "llm_pattern_mismatch"
        elif candidate_tone != tone_decision.tone_key:
            fallback_reason = "llm_tone_mismatch"
        elif not candidate_valid:
            fallback_reason = "llm_message_failed_safety_validation"
        else:
            message = candidate_message
            source = "llm_generated"

    validation_passed = candidate_valid if source == "llm_generated" else fallback_valid
    validation_errors = candidate_errors if source == "llm_generated" else fallback_errors
    metadata = {
        "enabled": True,
        "message_source": source,
        "llm_generation_reason": candidate_reason,
        "llm_message_candidate": candidate_message,
        "llm_tone_key": candidate_tone,
        "llm_pattern_code": candidate_pattern,
        "llm_safety_notes": candidate_safety_notes,
        "llm_message_validation": {"passed": candidate_valid, "errors": candidate_errors},
        "fallback_message": fallback_message,
        "fallback_reason": fallback_reason,
    }
    return MessageResolution(
        message=message if validation_passed else "",
        metadata=metadata,
        validation={"passed": validation_passed, "errors": validation_errors},
    )


def extract_llm_hybrid_candidate(response: AgentResponse) -> dict[str, Any]:
    direct = response.structured_payload.get("missed_dose_hybrid")
    if isinstance(direct, dict):
        return direct
    model_output = response.structured_payload.get("model_output")
    if isinstance(model_output, dict):
        nested = model_output.get("missed_dose_hybrid")
        if isinstance(nested, dict):
            return nested
        return {
            "pattern_code": model_output.get("pattern_code"),
            "pattern_confidence": model_output.get("pattern_confidence"),
            "judgement_reason": model_output.get("judgement_reason"),
            "reason": model_output.get("generation_reason"),
            "tone_key": model_output.get("tone_key"),
            "safety_notes": model_output.get("safety_notes"),
        }
    return {}


def rule_pattern_confidence(decision: AdherencePatternDecision) -> tuple[float, bool, str]:
    metrics = decision.streak_metrics
    if decision.pattern_code in {"C", "D", "E"}:
        return 0.95, False, "safety_or_escalation_rule_matched"
    if decision.pattern_code == "A":
        return 0.92, False, "long_taken_streak_rule_matched"
    if 1 <= metrics.current_consecutive_missed_days <= 2 and 7 <= metrics.previous_consecutive_taken_days < 14:
        return 0.58, True, "a_b_boundary_previous_taken_streak_7_to_13"
    if (
        metrics.prescription_day_count >= 15
        and metrics.current_consecutive_missed_days >= 2
        and 0.5 <= metrics.overall_adherence_rate <= 0.55
    ):
        return 0.6, True, "e_boundary_overall_adherence_near_threshold"
    return 0.82, False, "habit_not_formed_rule_matched"


def _decision_for_code(rule_decision: AdherencePatternDecision, pattern_code: str, llm_reason: str) -> AdherencePatternDecision:
    return AdherencePatternDecision(
        pattern_code=pattern_code,
        pattern_label=PATTERN_LABELS[pattern_code],
        tone=PATTERN_TONES[pattern_code],
        message=PATTERN_MESSAGES[pattern_code],
        reason=llm_reason or f"LLM 보조 판정으로 {pattern_code} 패턴을 선택했습니다.",
        streak_metrics=rule_decision.streak_metrics,
        escalation_stub_required=pattern_code in {"D", "E"},
    )


def _clean_pattern_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    return code if code in PATTERN_CODES else ""


def _clean_message(value: Any) -> str:
    return str(value or "").strip()


def _float_value(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, parsed))


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
