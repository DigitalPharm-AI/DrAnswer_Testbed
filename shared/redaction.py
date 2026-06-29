from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

SECRET_KEY_MARKERS = (
    "authorization",
    "bearer",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
    "access_key",
)

SAFE_TOKEN_METRIC_KEYS = {
    "input_tokens",
    "llm_max_tokens",
    "max_tokens",
    "output_tokens",
    "token_count",
    "token_usage",
    "tokens_per_run",
}

HASHED_IDENTIFIER_KEYS = {
    "patient_id",
    "phr_patient_key",
    "medical_record_number",
    "mrn",
    "resident_registration_number",
    "rrn",
    "ssn",
}

CLINICAL_TEXT_KEYS = {
    "answer",
    "advice",
    "body",
    "content",
    "description",
    "detail",
    "evidence",
    "error",
    "error_message",
    "food_name",
    "human_summary",
    "input_symptom",
    "last_error",
    "matched_effects",
    "matched_items",
    "matched_korean_symptom_name",
    "matched_symptom_term",
    "meal_name",
    "medication_name",
    "message",
    "notes",
    "object_label",
    "question",
    "query",
    "recent_chat",
    "reason",
    "request_message",
    "result",
    "result_message",
    "observations",
    "summary",
    "symptom_text",
    "user_input",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d .-]{7,}\d)\b")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_SECRET_RE = re.compile(
    r"(?i)\b(token|api[_-]?key|secret|password|authorization)\s*[:=]\s*['\"]?[^'\"\s,}]+"
)


def stable_hash(value: Any) -> str:
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def redact_for_logging(value: Any, *, key: str | None = None) -> Any:
    key_lower = (key or "").lower()
    if isinstance(value, dict) and _is_redacted_payload(value):
        return value
    if key_lower in HASHED_IDENTIFIER_KEYS:
        return _hashed_identifier_payload(value)
    if key_lower and _is_secret_key(key_lower):
        return _presence_payload(value)
    if key_lower in CLINICAL_TEXT_KEYS:
        return _clinical_text_payload(value)
    if isinstance(value, dict):
        return {str(item_key): redact_for_logging(item_value, key=str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [redact_for_logging(item) for item in value]
    if isinstance(value, str):
        return redact_inline_secrets(value)
    return value


def _is_redacted_payload(value: Mapping[str, Any]) -> bool:
    return value.get("redacted") is True and value.get("type") in {
        "clinical_text",
        "clinical_text_list",
        "clinical_text_object",
        "identifier",
        "secret",
    }


def safe_log_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in arguments.items():
        key_text = str(key)
        key_lower = key_text.lower()
        if key_lower in HASHED_IDENTIFIER_KEYS:
            safe[key_text] = redact_for_logging(value, key=key_text)
        elif key_lower and _is_secret_key(key_lower):
            safe[f"{key_text}_present"] = bool(value)
        elif isinstance(value, list):
            safe[key_text] = redact_for_logging(value, key=key_text) if key_lower in CLINICAL_TEXT_KEYS else {"count": len(value)}
        elif isinstance(value, dict):
            safe[key_text] = redact_for_logging(value, key=key_text) if key_lower in CLINICAL_TEXT_KEYS else {"keys": sorted(str(item) for item in value.keys())}
        else:
            safe[key_text] = redact_for_logging(value, key=key_text)
    return safe


def redact_inline_secrets(text: str, *, limit: int = 300) -> str:
    redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", text)
    redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    redacted = _BEARER_RE.sub(r"\1[REDACTED_TOKEN]", redacted)
    redacted = _ASSIGNMENT_SECRET_RE.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", redacted)
    return redacted if len(redacted) <= limit else f"{redacted[: limit - 3]}..."


def redacted_clinical_text_label(value: Any, *, key: str = "human_summary") -> str:
    redacted = redact_for_logging(value, key=key)
    if isinstance(redacted, dict) and redacted.get("type") == "clinical_text":
        return (
            "clinical text redacted"
            f" · len {redacted.get('length', 0)}"
            f" · sha256 {redacted.get('sha256', '')}"
        )
    return redact_inline_secrets(str(redacted), limit=220)


def safe_exception_summary(exc: BaseException | Any, *, limit: int = 220) -> str:
    error_type = str(getattr(exc, "error_type", "") or type(exc).__name__ or "Exception")
    raw_message = str(getattr(exc, "message", "") or str(exc) or "")
    if not raw_message:
        return error_type[:limit]
    redacted_message = redacted_clinical_text_label(raw_message, key="error_message")
    summary = f"{error_type}: {redacted_message}"
    return summary if len(summary) <= limit else f"{summary[: limit - 3]}..."


def _is_secret_key(key_lower: str) -> bool:
    if key_lower in SAFE_TOKEN_METRIC_KEYS:
        return False
    return key_lower in HASHED_IDENTIFIER_KEYS or any(marker in key_lower for marker in SECRET_KEY_MARKERS)


def _presence_payload(value: Any) -> dict[str, Any]:
    return {
        "redacted": True,
        "present": bool(value),
        "type": "secret",
    }


def _hashed_identifier_payload(value: Any) -> dict[str, Any]:
    return {
        "redacted": True,
        "present": bool(value),
        "type": "identifier",
        "sha256": stable_hash(value) if value else "",
    }


def _clinical_text_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, list):
        return {
            "redacted": True,
            "type": "clinical_text_list",
            "item_count": len(value),
        }
    if isinstance(value, dict):
        return {
            "redacted": True,
            "type": "clinical_text_object",
            "keys": sorted(str(item) for item in value.keys()),
        }
    text = "" if value is None else str(value)
    return {
        "redacted": True,
        "present": bool(text),
        "type": "clinical_text",
        "length": len(text),
        "sha256": stable_hash(text) if text else "",
    }
