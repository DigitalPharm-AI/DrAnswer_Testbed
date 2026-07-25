from __future__ import annotations

from typing import Any

from agent_app.tools.names import GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE
from shared.schemas import ToolCallResult

SYMPTOM_NORMALIZATION_HINTS = (
    (("메스꺼", "구역", "울렁거", "속울렁", "속불편"), "메스꺼움"),
    (("어지럽", "현기증", "핑돌"), "어지러움"),
    (("구토", "토했", "토할"), "구토"),
    (("설사",), "설사"),
    (("복통", "배아", "배가아"), "복통"),
    (("근육통", "근육이아", "쑤심"), "근육통"),
    (("두통", "머리아"), "두통"),
    (("피로감", "심한피로", "기운없"), "피로, 피곤함, 또는 기운 없음"),
)


def positive_side_effect_lookup(result: ToolCallResult) -> bool:
    return result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT and result.status == "success" and result.response.get("suspected") is True


def ae_tool_call_from_lookup(tool_call: dict[str, Any], result: ToolCallResult, payload: dict[str, Any]) -> dict[str, Any]:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    symptom_text = _text(arguments.get("symptom_text")) or _text(payload.get("message")) or _text(result.response.get("evidence")) or "증상"
    matched_effects = result.response.get("matched_effects") if isinstance(result.response.get("matched_effects"), list) else []
    symptom_normalize = _normalize_symptom_candidate(*matched_effects, symptom_text, result.response.get("evidence")) or symptom_text
    return {
        "name": GET_PRO_CTCAE_QUESTIONNAIRE,
        "arguments": {
            "symptom_text": symptom_text,
            "symptom_normalize": symptom_normalize,
        },
    }


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


def _compact(value: str) -> str:
    return "".join(str(value).split())


def _normalize_symptom_candidate(*values: Any) -> str:
    fallback = ""
    for value in values:
        text = _text(value)
        if not text:
            continue
        compact_text = _compact(text)
        for aliases, canonical in SYMPTOM_NORMALIZATION_HINTS:
            if any(_compact(alias) in compact_text for alias in aliases):
                return canonical
        if not fallback and "주의사항 관련 증상" not in text:
            fallback = text
    return fallback
