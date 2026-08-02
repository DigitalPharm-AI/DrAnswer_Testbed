from __future__ import annotations

from typing import Any

from shared.schemas import ToolCallResult
from shared.tool_names import GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE


def positive_side_effect_lookup(result: ToolCallResult) -> bool:
    return (
        result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT
        and result.status == "success"
        and result.response.get("suspected") is True
        and result.response.get("requires_clarification") is not True
    )


def ae_tool_calls_from_lookup(
    result: ToolCallResult,
    *,
    source_tool_call: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    assessments = result.response.get("assessments")
    if not isinstance(assessments, list):
        source_arguments = (
            source_tool_call.get("arguments")
            if isinstance(source_tool_call, dict)
            and isinstance(source_tool_call.get("arguments"), dict)
            else {}
        )
        symptom_text = (
            _text(source_arguments.get("symptom_text"))
            or _text((payload or {}).get("message"))
        )
        assessments = (
            [
                {
                    "match_status": "MATCHED",
                    "suspected": result.response.get("suspected"),
                    "symptom_text": symptom_text,
                }
            ]
            if symptom_text
            else []
        )
    calls: list[dict[str, Any]] = []
    seen: set[str] = set()
    for assessment in assessments:
        if (
            not isinstance(assessment, dict)
            or assessment.get("match_status") != "MATCHED"
            or assessment.get("suspected") is not True
        ):
            continue
        symptom_text = _text(assessment.get("symptom_text"))
        concept = assessment.get("matched_concept")
        concept_id = (
            _text(concept.get("concept_id"))
            if isinstance(concept, dict)
            else ""
        )
        fingerprint = concept_id or symptom_text.casefold()
        if not symptom_text or fingerprint in seen:
            continue
        seen.add(fingerprint)
        calls.append(
            {
                "name": GET_PRO_CTCAE_QUESTIONNAIRE,
                "arguments": {"symptom_text": symptom_text},
            }
        )
    return calls


def side_effect_pro_ctcae_summary(results: list[ToolCallResult]) -> str:
    lookup = next(
        (
            result
            for result in reversed(results)
            if result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT and result.status == "success" and result.response.get("suspected") is True
        ),
        None,
    )
    if lookup is None:
        return ""
    items = _unique_texts(lookup.response.get("matched_items"))
    effects = _effect_names(lookup.response.get("matched_effects"))
    item_label = ", ".join(items) if items else "복용 중인 약"
    effect_label = ", ".join(effects) if effects else "말씀하신 증상"
    return (
        f"현재 복용 중인 {item_label} 주의사항에서 {effect_label} 관련 가능성이 확인되었습니다. "
        "부작용으로 단정할 수는 없지만, 더 정확히 상태를 기록하기 위해 PRO-CTCAE 자기보고 문항을 준비했습니다."
    )


def _unique_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    texts = [str(item).strip() for item in value if str(item).strip()]
    return list(dict.fromkeys(texts))


def _effect_names(value: Any) -> list[str]:
    names = []
    for text in _unique_texts(value):
        name = text.split(":", 1)[1].strip() if ":" in text else text
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
