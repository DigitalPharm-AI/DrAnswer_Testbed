from __future__ import annotations

from typing import Any

from shared.schemas import SideEffectAssessmentResult


REFERENCE_PRECAUTIONS: dict[str, dict[str, Any]] = {
    "혈압약": {
        "severity": "high",
        "keywords": (
            "어지럽",
            "현기증",
            "핑 돌",
            "두근",
            "심장이 빨리",
            "심박",
            "흉통",
            "가슴 통증",
            "숨가쁨",
            "호흡곤란",
            "실신",
        ),
    },
    "당뇨약": {
        "severity": "high",
        "keywords": (
            "식은땀",
            "떨림",
            "심한 허기",
            "허기",
            "어지럽",
            "두근",
            "메스꺼",
            "속이",
            "속 불편",
            "구역",
            "복통",
        ),
    },
    "항암제": {
        "severity": "high",
        "keywords": (
            "메스꺼",
            "구역",
            "구토",
            "식욕",
            "식사량",
            "식사를 거의",
        ),
    },
    "고지혈증약": {
        "severity": "high",
        "keywords": (
            "근육통",
            "근육이 아",
            "쑤심",
            "황달",
            "소변색",
            "피로감",
            "심한 피로",
        ),
    },
    "비타민D": {
        "severity": "moderate",
        "keywords": ("구역", "구토", "변비", "갈증", "속 불편", "속이"),
    },
    "영양제": {
        "severity": "low",
        "keywords": ("메스꺼", "속 불편", "속이", "복통"),
    },
    "소화제": {
        "severity": "moderate",
        "keywords": ("설사", "복통", "두드러기", "가려움"),
    },
}

TREATMENT_AREA_ALIASES = {
    "고혈압약": "혈압약",
    "고혈압 치료약": "혈압약",
    "신장암 치료약": "항암제",
    "유방암 치료약": "항암제",
    "항암 치료약": "항암제",
}

MEDICATION_REFERENCE_ALIASES = {
    "암로디핀": "혈압약",
    "메트포르민": "당뇨약",
    "수니티닙": "항암제",
    "레트로졸": "항암제",
}

SEVERITY_RANK = {"none": 0, "low": 1, "moderate": 2, "high": 3}
SIDE_EFFECT_NORMALIZATION_HINTS = (
    (("메스꺼", "구역", "울렁거", "속울렁", "속 불편", "속불편", "속이"), "메스꺼움"),
    (("어지럽", "현기증", "핑 돌", "핑돌"), "어지러움"),
    (("구토",), "구토"),
    (("설사",), "설사"),
    (("복통",), "복통"),
    (("근육통", "근육이 아", "쑤심"), "근육통"),
    (("두통",), "두통"),
    (("피로감", "심한 피로"), "피로, 피곤함, 또는 기운 없음"),
    (("두근", "심장이 빨리", "심박"), "두근거림"),
    (("흉통", "가슴 통증"), "흉통"),
    (("숨가쁨", "호흡곤란"), "숨가쁨"),
)


def assess_side_effect_from_snapshot(
    *,
    symptom_text: str,
    patient_snapshot: dict[str, Any],
    medication_name: str | None = None,
) -> SideEffectAssessmentResult:
    medications = _active_medications(patient_snapshot)
    if medication_name:
        medications = [
            medication
            for medication in medications
            if _same_medication(str(medication["medication_name"]), medication_name)
        ]
    if not medications:
        raise ValueError("patient_medication_context_not_found")

    matched_effects: list[str] = []
    matched_items: list[str] = []
    evidence_parts: list[str] = []
    severity = "none"
    for medication in medications:
        display_name = str(medication["medication_name"])
        reference_name = _reference_name(medication)
        reference = REFERENCE_PRECAUTIONS.get(reference_name)
        if reference is None:
            continue
        hits = _matched_keywords(symptom_text, tuple(reference["keywords"]))
        if not hits:
            continue
        matched_items.append(display_name)
        effects = _canonical_effects(hits)
        matched_effects.extend(f"{display_name}: {effect}" for effect in effects)
        evidence_parts.append(
            f"{display_name}의 {reference_name} 기준정보에서 "
            f"증상 표현({', '.join(hits)})이 확인됨"
        )
        candidate_severity = str(reference["severity"])
        if SEVERITY_RANK.get(candidate_severity, 0) > SEVERITY_RANK.get(severity, 0):
            severity = candidate_severity

    matched_items = list(dict.fromkeys(matched_items))
    matched_effects = list(dict.fromkeys(matched_effects))
    suspected = bool(matched_items)
    if suspected:
        evidence = " / ".join(evidence_parts)
        recommendation = (
            "현재 복약 정보와 의약품 부작용 기준정보에서 관련 가능성이 확인되었습니다. "
            "증상 발생 시점과 현재 상태를 추가로 평가하세요."
        )
    else:
        evidence = (
            "현재 활성 복약 정보와 의약품 부작용 기준정보에서 "
            "직접 일치하는 표현은 확인되지 않았습니다."
        )
        recommendation = (
            "관련성이 확인되지 않았더라도 증상 발생 시점과 지속 여부를 추가로 확인하세요."
        )
    return SideEffectAssessmentResult(
        suspected=suspected,
        matched_effects=matched_effects,
        matched_items=matched_items,
        severity=severity,
        evidence=evidence,
        recommendation=recommendation,
    )


def side_effect_record_draft_from_snapshot(
    *,
    symptom_text: str,
    symptom_onset_text: str,
    medication_name: str | None,
    patient_snapshot: dict[str, Any],
    trace_id: str,
    source_event_type: str,
) -> dict[str, Any]:
    result = assess_side_effect_from_snapshot(
        symptom_text=symptom_text,
        patient_snapshot=patient_snapshot,
        medication_name=medication_name,
    )
    # Do not force the patient to attribute the symptom to one medication.
    # A singular medication_name is retained only when it came from the
    # patient's own expression. All Tool matches remain in matched_items.
    resolved_medication_name = medication_name or ""

    return {
        "medication_name": resolved_medication_name or None,
        "symptom_text": symptom_text,
        "symptom_onset_text": symptom_onset_text,
        "suspected": result.suspected,
        "matched_effects": result.matched_effects,
        "matched_items": result.matched_items,
        "related_dose_event_id": _related_dose_event_id(
            patient_snapshot,
            resolved_medication_name,
        )
        if resolved_medication_name
        else None,
    }


def _active_medications(patient_snapshot: dict[str, Any]) -> list[dict[str, str]]:
    raw = patient_snapshot.get("active_medication_schedules")
    if not isinstance(raw, list):
        return []
    medications: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("medication_name") or "").strip()
        treatment_area = str(item.get("treatment_area") or "").strip()
        if not name:
            continue
        key = (name, treatment_area)
        if key in seen:
            continue
        seen.add(key)
        medications.append(
            {
                "medication_name": name,
                "treatment_area": treatment_area,
            }
        )
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


def _reference_name(medication: dict[str, str]) -> str:
    treatment_area = medication.get("treatment_area", "")
    if treatment_area in REFERENCE_PRECAUTIONS:
        return treatment_area
    if treatment_area in TREATMENT_AREA_ALIASES:
        return TREATMENT_AREA_ALIASES[treatment_area]
    medication_name = medication.get("medication_name", "")
    return next(
        (
            reference
            for alias, reference in MEDICATION_REFERENCE_ALIASES.items()
            if alias in medication_name
        ),
        treatment_area,
    )


def _same_medication(candidate: str, requested: str) -> bool:
    normalized_candidate = candidate.replace(" ", "").lower()
    normalized_requested = requested.replace(" ", "").lower()
    return (
        normalized_requested in normalized_candidate
        or normalized_candidate in normalized_requested
    )


def _matched_keywords(symptom_text: str, keywords: tuple[str, ...]) -> list[str]:
    compact_symptom = symptom_text.replace(" ", "")
    return [
        keyword
        for keyword in keywords
        if keyword.replace(" ", "") in compact_symptom
    ]


def _canonical_effects(hits: list[str]) -> list[str]:
    effects: list[str] = []
    for hit in hits:
        compact_hit = hit.replace(" ", "")
        canonical = next(
            (
                effect
                for aliases, effect in SIDE_EFFECT_NORMALIZATION_HINTS
                if any(
                    alias.replace(" ", "") in compact_hit
                    or compact_hit in alias.replace(" ", "")
                    for alias in aliases
                )
            ),
            hit.strip(),
        )
        if canonical and canonical not in effects:
            effects.append(canonical)
    return effects
