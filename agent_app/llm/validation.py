from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_app.orchestration.delegation import DELEGATION_TOOL_NAMES
from agent_app.tools.protocol import ALLOWED_TOOL_NAMES

ALLOWED_PATTERN_CODES = {"A", "B", "C", "D", "E", "unknown", ""}
ALLOWED_TONE_KEYS = {"empathy", "persuasion", "practical", "warning_soft", "side_effect_check", ""}
FORBIDDEN_MESSAGE_TERMS = ("암", "당뇨", "혈압", "항암", "위험", "큰일", "반드시", "무조건", "죽")


class LlmToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def normalize_native_tool_arguments(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
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


class MissedDoseGenerationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_message: str = Field(min_length=1, max_length=45)
    token_usage: dict[str, int] | None = None

    @field_validator("generated_message")
    @classmethod
    def validate_generated_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("generated_message_required")
        return text


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
        if not (self.patient_message or self.message or self.missed_dose_hybrid or self.tool_call or self.tool_calls):
            raise ValueError("missed_dose_output_requires_patient_content")
        return self


class MultiturnChatOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    message: str | None = None
    tool_call: LlmToolCall | None = None
    tool_calls: list[LlmToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_content(self) -> MultiturnChatOutput:
        if not (
            self.message or self.tool_call or self.tool_calls
        ):
            raise ValueError("multiturn_output_requires_message_or_tool_call")
        return self


class MutationConfirmationReplyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Literal["confirm", "cancel", "revise", "unclear", "new_request"]
    message: str

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("mutation_confirmation_reply_message_required")
        return text


def validate_llm_output(
    decision_type: str,
    output: dict[str, Any],
    payload: dict[str, Any],
    *,
    allow_tool_only: bool = False,
) -> dict[str, Any]:
    if decision_type == "pattern_analysis":
        DailyPatternOutput.model_validate(output)
    elif decision_type == "missed_dose_assessment":
        MissedDoseOutput.model_validate(output)
        if not (allow_tool_only and _has_tool_call(output)):
            _validate_missed_dose_contextual_requirements(output, payload)
    elif decision_type == "missed_dose_message_generation":
        validate_missed_dose_generation_output(output)
    elif decision_type == "system_guidance":
        MultiturnChatOutput.model_validate(output)
    return output


def validate_mutation_confirmation_reply_output(output: dict[str, Any]) -> dict[str, Any]:
    semantic_output = {
        key: value
        for key, value in output.items()
        if key != "token_usage"
    }
    validated = MutationConfirmationReplyOutput.model_validate(
        semantic_output
    ).model_dump(mode="json")
    if isinstance(output.get("token_usage"), dict):
        validated["token_usage"] = dict(output["token_usage"])
    return validated


def validate_missed_dose_generation_output(
    output: dict[str, Any],
) -> dict[str, Any]:
    return MissedDoseGenerationOutput.model_validate(output).model_dump(
        mode="json",
        exclude_none=True,
    )



def _has_tool_call(output: dict[str, Any]) -> bool:
    return isinstance(output.get("tool_call"), dict) or (
        isinstance(output.get("tool_calls"), list) and bool(output.get("tool_calls"))
    )

def _validate_missed_dose_contextual_requirements(output: dict[str, Any], payload: dict[str, Any]) -> None:
    if not (_has_context(payload, "tone_policy_context") or _has_context(payload, "adherence_pattern_context")):
        return
    raw = output.get("missed_dose_hybrid")
    if not isinstance(raw, dict):
        raise ValueError("missed_dose_hybrid_required")
    MissedDoseHybridOutput.model_validate(raw)


def _has_context(payload: dict[str, Any], key: str) -> bool:
    return isinstance(payload.get(key), dict) and bool(payload.get(key))
