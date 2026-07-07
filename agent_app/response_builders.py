from __future__ import annotations

from typing import Any


def string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            normalized.append(item)
        elif isinstance(item, dict):
            text = item.get("question") or item.get("category") or item.get("text")
            if text:
                normalized.append(str(text))
    return normalized


def text_field(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        content = value.get("content") or value.get("text") or value.get("body")
        if content is not None:
            return str(content).strip()
    return ""


def natural_chat_summary(output: dict[str, Any]) -> str:
    for key in ("advice", "message", "answer", "response", "patient_message"):
        text = text_field(output.get(key))
        if text:
            return text
    return ""


def missed_dose_hybrid_payload(output: dict[str, Any]) -> dict[str, Any]:
    raw = output.get("missed_dose_hybrid")
    if not isinstance(raw, dict):
        raw = {}
    payload = {
        "reason": raw.get("reason") or output.get("generation_reason"),
        "generated_message": raw.get("generated_message"),
        "pattern_code": raw.get("pattern_code") or output.get("pattern_code"),
        "pattern_confidence": raw.get("pattern_confidence") or output.get("pattern_confidence"),
        "judgement_reason": raw.get("judgement_reason") or output.get("judgement_reason"),
        "tone_key": raw.get("tone_key") or output.get("tone_key"),
        "safety_notes": raw.get("safety_notes") or output.get("safety_notes"),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}
