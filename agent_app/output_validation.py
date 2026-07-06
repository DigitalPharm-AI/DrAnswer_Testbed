from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_app.agent_delegation import DELEGATION_TOOL_NAMES
from agent_app.tool_protocol import ALLOWED_TOOL_NAMES

ALLOWED_PATTERN_CODES = {"A", "B", "C", "D", "E", "unknown", ""}
ALLOWED_TONE_KEYS = {"empathy", "persuasion", "practical", "warning_soft", "side_effect_check", ""}
FORBIDDEN_MESSAGE_TERMS = ("암", "당뇨", "혈압", "항암", "위험", "큰일", "반드시", "무조건", "죽")


class LlmToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_tool_shape(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        if not normalized.get("name") and normalized.get("tool_name"):
            normalized["name"] = normalized["tool_name"]
        if not isinstance(normalized.get("arguments"), dict) and isinstance(normalized.get("args"), dict):
            normalized["arguments"] = normalized["args"]
        return normalized

    @field_validator("name")
    @classmethod
    def validate_tool_name(cls, value: str) -> str:
        if value not in ALLOWED_TOOL_NAMES and value not in DELEGATION_TOOL_NAMES:
            raise ValueError(f"unsupported_tool:{value}")
        return value


class MissedDoseHybridOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    reason: str = ""
    generated_message: str
    pattern_code: str = ""
    tone_key: str = ""
    safety_notes: list[str] = Field(default_factory=list)

    @field_validator("generated_message")
    @classmethod
    def validate_generated_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("generated_message_required")
        if len(text) > 60:
            raise ValueError("generated_message_too_long")
        forbidden = [term for term in FORBIDDEN_MESSAGE_TERMS if term in text]
        if forbidden:
            raise ValueError(f"generated_message_forbidden_terms:{','.join(forbidden)}")
        return text

    @field_validator("pattern_code")
    @classmethod
    def validate_pattern_code(cls, value: str) -> str:
        if value not in ALLOWED_PATTERN_CODES:
            raise ValueError(f"unsupported_pattern_code:{value}")
        return value

    @field_validator("tone_key")
    @classmethod
    def validate_tone_key(cls, value: str) -> str:
        if value not in ALLOWED_TONE_KEYS:
            raise ValueError(f"unsupported_tone_key:{value}")
        return value


class DailyPatternOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str | None = None
    message: str | None = None
    tool_call: LlmToolCall | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> DailyPatternOutput:
        if not (self.summary or self.message or self.tool_call or self.tool_calls):
            raise ValueError("daily_pattern_output_requires_summary_or_tool_call")
        return self


class MissedDoseOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    patient_message: str | None = None
    message: str | None = None
    missed_dose_hybrid: MissedDoseHybridOutput | None = None
    follow_up_questions: list[Any] = Field(default_factory=list)
    tool_call: LlmToolCall | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> MissedDoseOutput:
        if not (self.patient_message or self.message or self.missed_dose_hybrid or self.follow_up_questions or self.tool_call or self.tool_calls):
            raise ValueError("missed_dose_output_requires_patient_content")
        return self


class MultiturnChatOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    advice: str | None = None
    message: str | None = None
    observations: list[Any] = Field(default_factory=list)
    tool_call: LlmToolCall | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> MultiturnChatOutput:
        if not (self.advice or self.message or self.tool_call or self.tool_calls):
            raise ValueError("multiturn_output_requires_reply_or_tool_call")
        return self


def validate_llm_output(decision_type: str, output: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if decision_type == "pattern_analysis":
        DailyPatternOutput.model_validate(output)
    elif decision_type == "missed_dose_assessment":
        MissedDoseOutput.model_validate(output)
        _validate_missed_dose_contextual_requirements(output, payload)
    elif decision_type == "system_guidance":
        MultiturnChatOutput.model_validate(output)
    return output


def safe_fallback_output(decision_type: str, payload: dict[str, Any], validation_error: Exception) -> dict[str, Any]:
    reason = f"{type(validation_error).__name__}: {validation_error}"
    if decision_type == "pattern_analysis":
        return {
            "summary": "복약 패턴을 확인했습니다.",
            "observations": ["llm_output_validation_fallback"],
            "validation_error": reason,
        }
    if decision_type == "missed_dose_assessment":
        tone_key = _tone_key(payload)
        pattern_code = _pattern_code(payload)
        return {
            "patient_message": "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
            "likely_reason": "unknown",
            "side_effect_signal": False,
            "follow_up_questions": ["현재 복용 가능하신 상태인가요?"],
            "recommendation": "복용 가능 여부와 미복용 이유를 먼저 확인하세요.",
            "missed_dose_hybrid": {
                "reason": "LLM 출력 검증 실패로 안전 기본 문구를 사용했습니다.",
                "generated_message": "복약 루틴을 함께 맞춰봐요. 지금 확인해보세요.",
                "pattern_code": pattern_code,
                "tone_key": tone_key,
                "safety_notes": ["fallback", "no_medication_name", "no_diagnosis", "non_directive"],
            },
            "validation_error": reason,
        }
    return {
        "message": "요청을 확인했습니다. 잠시 후 다시 안내할게요.",
        "observations": ["llm_output_validation_fallback"],
        "validation_error": reason,
    }


def _validate_missed_dose_contextual_requirements(output: dict[str, Any], payload: dict[str, Any]) -> None:
    if not (_has_context(payload, "tone_policy_context") or _has_context(payload, "adherence_pattern_context")):
        return
    raw = output.get("missed_dose_hybrid")
    if not isinstance(raw, dict):
        raise ValueError("missed_dose_hybrid_required")
    MissedDoseHybridOutput.model_validate(raw)


def _has_context(payload: dict[str, Any], key: str) -> bool:
    return isinstance(payload.get(key), dict) and bool(payload.get(key))


def _tone_key(payload: dict[str, Any]) -> str:
    context = payload.get("tone_policy_context") if isinstance(payload.get("tone_policy_context"), dict) else {}
    return str(context.get("tone_key") or "")


def _pattern_code(payload: dict[str, Any]) -> str:
    context = payload.get("adherence_pattern_context") if isinstance(payload.get("adherence_pattern_context"), dict) else {}
    return str(context.get("pattern_code") or "")
