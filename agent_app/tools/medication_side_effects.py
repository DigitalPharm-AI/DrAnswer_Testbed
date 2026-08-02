from __future__ import annotations

from typing import Any

import anyio

from agent_app.ae_pro_ctcae import (
    KNOWN_SYMPTOM_ALIASES,
    ProCtcaeReferenceUnavailable,
    match_pro_ctcae_symptom,
    pro_ctcae_equivalent_aliases_for_reference_term,
)
from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.semantic_verifier import SemanticMatchVerifier
from agent_app.persistence.adverse_reaction_repository import (
    AdverseReactionLookup,
    AgentAdverseReactionRepository,
    MedicationReactionLookup,
)
from agent_app.persistence.symptom_concept_repository import (
    ClinicalSymptomConceptMatch,
    SymptomConceptRepository,
)
from shared.schemas import SideEffectAssessmentResult


def assess_side_effect_from_snapshot(
    *,
    symptom_text: str,
    patient_snapshot: dict[str, Any],
    adverse_reactions: AdverseReactionLookup,
    medication_name: str | None = None,
) -> SideEffectAssessmentResult:
    medications = _active_medications(patient_snapshot)
    if medication_name:
        medications = [
            medication
            for medication in medications
            if _same_medication(
                str(medication["medication_name"]),
                medication_name,
            )
        ]
    if not medications:
        raise ValueError("patient_medication_context_not_found")

    symptom_terms = _symptom_lookup_terms(symptom_text)
    lookups: list[tuple[str, MedicationReactionLookup]] = []
    for medication in medications:
        display_name = str(medication["medication_name"])
        lookup = adverse_reactions.lookup_medication_reactions(
            medication_name=display_name,
            symptom_terms=symptom_terms,
        )
        lookups.append((display_name, lookup))
    return _assessment_from_lookups(lookups)


async def assess_side_effect_from_snapshot_semantic(
    *,
    symptom_text: str,
    patient_snapshot: dict[str, Any],
    adverse_reactions: AgentAdverseReactionRepository,
    embedding_provider: EmbeddingProvider,
    semantic_verifier: SemanticMatchVerifier,
    symptom_concept: ClinicalSymptomConceptMatch | None = None,
    symptom_concepts: SymptomConceptRepository | None = None,
    medication_name: str | None = None,
    top_k: int = 8,
    min_similarity: float = 0.35,
) -> SideEffectAssessmentResult:
    medications = _active_medications(patient_snapshot)
    if medication_name:
        medications = [
            medication
            for medication in medications
            if _same_medication(
                str(medication["medication_name"]),
                medication_name,
            )
        ]
    if not medications:
        raise ValueError("patient_medication_context_not_found")

    if symptom_concept is not None and symptom_concepts is not None:
        candidates = await anyio.to_thread.run_sync(
            lambda: symptom_concepts.linked_reaction_candidates(
                concept=symptom_concept,
                mfds_source_version=(
                    symptom_concepts.mfds_source_version()
                ),
            )
        )
    else:
        candidates = await adverse_reactions.resolve_reaction_candidates(
            symptom_text=symptom_text,
            embedding_provider=embedding_provider,
            semantic_verifier=semantic_verifier,
            top_k=top_k,
            min_similarity=min_similarity,
        )
        equivalent_terms: list[str] = []
        for candidate in candidates:
            equivalent_terms.extend(
                pro_ctcae_equivalent_aliases_for_reference_term(
                    candidate.normalized_term
                )
            )
        if equivalent_terms:
            candidates = await anyio.to_thread.run_sync(
                lambda: (
                    adverse_reactions.expand_reaction_candidates_by_exact_terms(
                        candidates=candidates,
                        equivalent_terms=equivalent_terms,
                        model_id=embedding_provider.identity.model_id,
                    )
                )
            )
    lookups: list[tuple[str, MedicationReactionLookup]] = []
    for medication in medications:
        display_name = str(medication["medication_name"])
        lookup = await anyio.to_thread.run_sync(
            lambda display_name=display_name: (
                adverse_reactions.lookup_medication_reactions_for_candidates(
                    medication_name=display_name,
                    candidates=candidates,
                )
            )
        )
        lookups.append((display_name, lookup))
    return _assessment_from_lookups(lookups)


def _assessment_from_lookups(
    lookups: list[tuple[str, MedicationReactionLookup]],
) -> SideEffectAssessmentResult:
    matched_effects: list[str] = []
    matched_items: list[str] = []
    reference_matches: list[dict[str, Any]] = []
    evidence_parts: list[str] = []
    reference_source = ""
    reference_status = ""

    for display_name, lookup in lookups:
        if lookup.source:
            reference_source = str(lookup.source.get("kind") or "")
            reference_status = str(lookup.source.get("review_status") or "")
        if not lookup.matches:
            continue
        matched_items.append(display_name)
        for match in lookup.matches:
            reaction = str(match.get("reaction_normalized") or "").strip()
            if not reaction:
                continue
            matched_effects.append(f"{display_name}: {reaction}")
            evidence_text = str(match.get("evidence_text") or "").strip()
            reference_matches.append(
                {
                    "medication_name": display_name,
                    "item_seq": str(match.get("item_seq") or ""),
                    "item_name": str(match.get("item_name") or ""),
                    "reaction": reaction,
                    "frequency": str(match.get("frequency_text") or ""),
                    "assertion": str(match.get("assertion") or ""),
                    "confidence": float(match.get("confidence") or 0.0),
                    "review_status": str(match.get("review_status") or ""),
                    "section_path": str(match.get("section_path") or ""),
                    "evidence_text": evidence_text,
                    "resolution_method": lookup.resolution_method,
                    "match_type": str(
                        match.get("semantic_match_type") or "exact"
                    ),
                    "similarity": float(
                        match.get("semantic_similarity") or 1.0
                    ),
                }
            )
            if evidence_text and len(evidence_parts) < 4:
                evidence_parts.append(f"{match.get('item_name') or display_name}: {evidence_text[:300]}")

    matched_items = list(dict.fromkeys(matched_items))
    matched_effects = list(dict.fromkeys(matched_effects))
    suspected = bool(matched_items)
    if suspected:
        evidence = " / ".join(evidence_parts) or ("식약처 허가사항의 부작용 섹션에서 증상 표현이 확인되었습니다.")
        recommendation = "식약처 허가사항에 기재된 증상과 일치합니다. 인과관계가 확정된 것은 아니므로 발생 시점과 현재 상태를 추가로 평가하세요."
    else:
        evidence = "현재 활성 복약 품목의 식약처 허가사항에서 직접 일치하는 부작용 표현은 확인되지 않았습니다."
        recommendation = "일치 항목이 없더라도 증상이 심하거나 지속되면 의료진 또는 약사에게 확인하세요."
    return SideEffectAssessmentResult(
        suspected=suspected,
        matched_effects=matched_effects,
        matched_items=matched_items,
        severity="moderate" if suspected else "none",
        evidence=evidence,
        recommendation=recommendation,
        reference_source=reference_source or "MFDS_LABEL_TRACE_DB",
        reference_status=(reference_status or "CANDIDATE_NOT_CLINICALLY_REVIEWED"),
        reference_matches=reference_matches,
    )


def side_effect_record_draft_from_snapshot(
    *,
    symptom_text: str,
    symptom_onset_text: str,
    medication_name: str | None,
    patient_snapshot: dict[str, Any],
    trace_id: str,
    source_event_type: str,
    adverse_reactions: AdverseReactionLookup | None = None,
    assessment: SideEffectAssessmentResult | None = None,
) -> dict[str, Any]:
    del trace_id, source_event_type
    if assessment is None:
        if adverse_reactions is None:
            raise ValueError("adverse_reaction_reference_required")
        assessment = assess_side_effect_from_snapshot(
            symptom_text=symptom_text,
            patient_snapshot=patient_snapshot,
            medication_name=medication_name,
            adverse_reactions=adverse_reactions,
        )
    # Do not force the patient to attribute the symptom to one medication.
    # A singular medication_name is retained only when it came from the
    # patient's own expression. All database matches remain in matched_items.
    resolved_medication_name = medication_name or ""

    return {
        "medication_name": resolved_medication_name or None,
        "symptom_text": symptom_text,
        "symptom_onset_text": symptom_onset_text,
        "suspected": assessment.suspected,
        "matched_effects": assessment.matched_effects,
        "matched_items": assessment.matched_items,
        "related_dose_event_id": _related_dose_event_id(
            patient_snapshot,
            resolved_medication_name,
        )
        if resolved_medication_name
        else None,
    }


def _symptom_lookup_terms(symptom_text: str) -> list[str]:
    terms = [symptom_text.strip()]
    try:
        assessment = match_pro_ctcae_symptom(symptom_text)
    except (ProCtcaeReferenceUnavailable, ValueError):
        return [term for term in terms if term]
    if assessment.matched:
        terms.extend(
            (
                assessment.matched_symptom_term,
                assessment.matched_korean_symptom_name,
            )
        )
        terms.extend(
            KNOWN_SYMPTOM_ALIASES.get(
                assessment.matched_symptom_term,
                (),
            )
        )
        terms.extend(
            KNOWN_SYMPTOM_ALIASES.get(
                assessment.matched_korean_symptom_name,
                (),
            )
        )
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _active_medications(
    patient_snapshot: dict[str, Any],
) -> list[dict[str, str]]:
    raw = patient_snapshot.get("active_medication_schedules")
    if not isinstance(raw, list):
        return []
    medications: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("medication_name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        medications.append({"medication_name": name})
    return medications


def _related_dose_event_id(
    patient_snapshot: dict[str, Any],
    medication_name: str,
) -> str | None:
    today_medication = patient_snapshot.get("today_medication")
    if not isinstance(today_medication, dict):
        return None
    events = today_medication.get("dose_events")
    if not isinstance(events, list):
        return None
    matching = [
        event
        for event in events
        if isinstance(event, dict)
        and _same_medication(
            str(event.get("medication_name") or ""),
            medication_name,
        )
        and str(event.get("dose_event_id") or "").strip()
    ]
    if not matching:
        return None
    matching.sort(
        key=lambda event: str(event.get("scheduled_for") or ""),
        reverse=True,
    )
    return str(matching[0]["dose_event_id"])


def _same_medication(candidate: str, requested: str) -> bool:
    normalized_candidate = candidate.replace(" ", "").lower()
    normalized_requested = requested.replace(" ", "").lower()
    return normalized_requested in normalized_candidate or normalized_candidate in normalized_requested
