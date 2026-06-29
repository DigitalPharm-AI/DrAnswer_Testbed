from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from phr_app.models import PhrItemPrecaution, PhrPatient, PhrPatientMedication, PhrSideEffectAssessment
from shared.json_utils import dump_json, parse_json_list
from shared.schemas import (
    PhrMedicationRegistrationItem,
    PhrPatientRegistrationRequest,
    PhrPatientRegistrationResult,
    PhrRegisteredMedication,
    SideEffectAssessmentRequest,
    SideEffectAssessmentResult,
)

SEED_ITEM_PRECAUTIONS = [
    (
        "혈압약",
        "복용 후 어지러움, 현기증, 두근거림, 심박 변화가 나타날 수 있습니다. 흉통, 가슴 통증, 숨가쁨, 호흡곤란, 실신감이 있으면 즉시 진료가 필요합니다.",
        "high",
        ["어지럽", "현기증", "핑 돌", "두근", "심장이 빨리", "심박", "흉통", "가슴 통증", "숨가쁨", "호흡곤란", "실신"],
    ),
    (
        "당뇨약",
        "저혈당 의심 증상으로 식은땀, 떨림, 심한 허기, 어지러움, 두근거림이 나타날 수 있습니다. 메스꺼움, 속 불편, 구역, 복통 같은 위장 증상도 확인이 필요합니다.",
        "high",
        ["식은땀", "떨림", "심한 허기", "허기", "어지럽", "두근", "메스꺼", "속이", "속 불편", "구역", "복통"],
    ),
    (
        "항암제",
        "복용 후 메스꺼움, 구역, 구토, 식욕 저하, 식사량 감소가 나타날 수 있습니다. 증상이 지속되거나 식사를 거의 못 하면 의료진에게 알려야 합니다.",
        "high",
        ["메스꺼", "구역", "구토", "식욕", "식사량", "식사를 거의"],
    ),
    (
        "고지혈증약",
        "근육통, 근육이 아픔, 쑤심 같은 근육 증상이 나타날 수 있습니다. 황달, 소변색 변화, 심한 피로감은 간 이상 가능성이 있어 확인이 필요합니다.",
        "high",
        ["근육통", "근육이 아", "쑤심", "황달", "소변색", "피로감", "심한 피로"],
    ),
    (
        "비타민D",
        "구역, 구토, 변비, 갈증, 속 불편 같은 증상이 지속되면 복용량과 병용 약물을 확인해야 합니다.",
        "moderate",
        ["구역", "구토", "변비", "갈증", "속 불편", "속이"],
    ),
    (
        "영양제",
        "공복 복용 시 메스꺼움, 속 불편, 복통이 나타날 수 있습니다. 증상이 지속되면 복용 시간이나 제품 성분을 확인해야 합니다.",
        "low",
        ["메스꺼", "속 불편", "속이", "복통"],
    ),
    (
        "소화제",
        "설사, 복통, 두드러기, 가려움 같은 이상 반응이 생기면 복용을 중단하고 상담이 필요할 수 있습니다.",
        "moderate",
        ["설사", "복통", "두드러기", "가려움"],
    ),
]

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


def seed_phr_data(session: Session) -> None:
    existing_items = set(session.scalars(select(PhrItemPrecaution.item_name)).all())
    for item_name, precautions_text, severity_hint, keywords in SEED_ITEM_PRECAUTIONS:
        if item_name not in existing_items:
            session.add(
                PhrItemPrecaution(
                    item_name=item_name,
                    precautions_text=precautions_text,
                    severity_hint=severity_hint,
                    keywords_json=dump_json(keywords),
                    active=True,
                )
            )
    session.commit()


def _normalize_medications(medications: list[PhrMedicationRegistrationItem]) -> list[PhrMedicationRegistrationItem]:
    normalized: list[PhrMedicationRegistrationItem] = []
    seen: set[str] = set()
    for medication in medications:
        item_name = medication.item_name.strip()
        if not item_name or item_name in seen:
            continue
        seen.add(item_name)
        normalized.append(PhrMedicationRegistrationItem(item_name=item_name, dosage=medication.dosage.strip()))
    return normalized


def _registered_medication_view(session: Session, medication: PhrPatientMedication) -> PhrRegisteredMedication:
    precaution = session.scalar(
        select(PhrItemPrecaution).where(
            PhrItemPrecaution.item_name == medication.item_name,
            PhrItemPrecaution.active.is_(True),
        )
    )
    return PhrRegisteredMedication(
        item_name=medication.item_name,
        dosage=medication.dosage,
        active=medication.active,
        precautions_text=precaution.precautions_text if precaution else "",
    )


def list_patient_medications(session: Session, phr_patient_key: str) -> list[dict]:
    rows = session.scalars(
        select(PhrPatientMedication)
        .where(PhrPatientMedication.phr_patient_key == phr_patient_key, PhrPatientMedication.active.is_(True))
        .order_by(PhrPatientMedication.item_name.asc())
    ).all()
    return [row.model_dump(mode="json") for row in [_registered_medication_view(session, medication) for medication in rows]]


def register_patient_medications(session: Session, request: PhrPatientRegistrationRequest) -> PhrPatientRegistrationResult:
    phr_patient_key = f"phr_{uuid4().hex}"
    session.add(PhrPatient(phr_patient_key=phr_patient_key, active=True))
    session.flush()
    _replace_patient_medications(session, phr_patient_key, request.medications)
    session.commit()
    return PhrPatientRegistrationResult(
        phr_patient_key=phr_patient_key,
        medications=[PhrRegisteredMedication.model_validate(row) for row in list_patient_medications(session, phr_patient_key)],
    )


def update_patient_medications(session: Session, phr_patient_key: str, request: PhrPatientRegistrationRequest) -> PhrPatientRegistrationResult:
    patient = session.scalar(select(PhrPatient).where(PhrPatient.phr_patient_key == phr_patient_key, PhrPatient.active.is_(True)))
    if patient is None:
        raise ValueError("phr_patient_not_found")
    _replace_patient_medications(session, phr_patient_key, request.medications)
    session.commit()
    return PhrPatientRegistrationResult(
        phr_patient_key=phr_patient_key,
        medications=[PhrRegisteredMedication.model_validate(row) for row in list_patient_medications(session, phr_patient_key)],
    )


def _replace_patient_medications(session: Session, phr_patient_key: str, medications: list[PhrMedicationRegistrationItem]) -> None:
    normalized = _normalize_medications(medications)
    session.execute(update(PhrPatientMedication).where(PhrPatientMedication.phr_patient_key == phr_patient_key).values(active=False))
    for medication in normalized:
        session.add(
            PhrPatientMedication(
                phr_patient_key=phr_patient_key,
                item_name=medication.item_name,
                dosage=medication.dosage,
                active=True,
            )
        )
    session.flush()


def _matched_keywords(symptom_text: str, keywords: list[str]) -> list[str]:
    compact_symptom = symptom_text.replace(" ", "")
    return [keyword for keyword in keywords if keyword.replace(" ", "") in compact_symptom]


def _canonical_effects(hits: list[str]) -> list[str]:
    effects: list[str] = []
    for hit in hits:
        compact_hit = hit.replace(" ", "")
        canonical = next(
            (
                effect
                for aliases, effect in SIDE_EFFECT_NORMALIZATION_HINTS
                if any(alias.replace(" ", "") in compact_hit or compact_hit in alias.replace(" ", "") for alias in aliases)
            ),
            hit.strip(),
        )
        if canonical and canonical not in effects:
            effects.append(canonical)
    return effects


def _matched_effect_labels(item_name: str, hits: list[str]) -> list[str]:
    return [f"{item_name}: {effect}" for effect in _canonical_effects(hits)]


def assess_side_effect(session: Session, request: SideEffectAssessmentRequest) -> SideEffectAssessmentResult:
    patient = session.scalar(select(PhrPatient).where(PhrPatient.phr_patient_key == request.phr_patient_key, PhrPatient.active.is_(True)))
    if patient is None:
        raise ValueError("phr_patient_not_found")

    medication_stmt = select(PhrPatientMedication).where(
        PhrPatientMedication.phr_patient_key == request.phr_patient_key,
        PhrPatientMedication.active.is_(True),
    )
    if request.medication_name:
        medication_stmt = medication_stmt.where(PhrPatientMedication.item_name == request.medication_name)
    medications = session.scalars(medication_stmt).all()
    patient_item_names = [medication.item_name for medication in medications]
    symptom_text = request.symptom_text.strip()

    matched_effects: list[str] = []
    matched_items: list[str] = []
    matched_precautions: list[str] = []
    evidence_parts: list[str] = []
    severity = "none"
    if patient_item_names:
        precautions = session.scalars(
            select(PhrItemPrecaution).where(
                PhrItemPrecaution.item_name.in_(patient_item_names),
                PhrItemPrecaution.active.is_(True),
            )
        ).all()
        for precaution in precautions:
            keywords = parse_json_list(precaution.keywords_json)
            hits = _matched_keywords(symptom_text, keywords)
            if not hits:
                continue
            matched_items.append(precaution.item_name)
            matched_effects.extend(_matched_effect_labels(precaution.item_name, hits))
            matched_precautions.append(precaution.precautions_text)
            evidence_parts.append(f"{precaution.item_name} 주의사항 키워드({', '.join(hits)})")
            if SEVERITY_RANK.get(precaution.severity_hint, 0) > SEVERITY_RANK.get(severity, 0):
                severity = precaution.severity_hint

    suspected = bool(matched_items)
    if not suspected:
        evidence = "현재 PHR의 환자 복용 품목 주의사항에서 직접 일치하는 표현은 확인되지 않았습니다."
        recommendation = "증상이 계속되거나 악화되면 의료진 또는 약사에게 확인하도록 안내하세요."
    elif severity == "high":
        evidence = " / ".join(evidence_parts)
        recommendation = "복용 중인 품목의 주의사항과 관련 가능성이 있습니다. 흉통/호흡곤란/실신감 같은 긴급 증상이 있으면 즉시 진료를 안내하세요."
    else:
        evidence = " / ".join(evidence_parts)
        recommendation = "복용 중인 품목의 주의사항과 관련 가능성이 있습니다. 증상 지속 시간과 복용 시점을 추가 확인하고 필요 시 의료진 또는 약사 상담을 안내하세요."

    result = SideEffectAssessmentResult(
        suspected=suspected,
        matched_effects=matched_effects,
        matched_items=matched_items,
        severity=severity,
        evidence=evidence,
        recommendation=recommendation,
    )
    session.add(
        PhrSideEffectAssessment(
            phr_patient_key=request.phr_patient_key,
            medication_name=request.medication_name or "",
            symptom_text=symptom_text,
            suspected=result.suspected,
            matched_effects_json=dump_json(result.matched_effects),
            matched_items_json=dump_json(result.matched_items),
            matched_precautions_json=dump_json(matched_precautions),
            severity=result.severity,
            evidence=result.evidence,
            recommendation=result.recommendation,
        )
    )
    session.commit()
    return result
