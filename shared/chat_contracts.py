from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas import AgentResponse

RequestedReturnType = Literal["selection_box", "input_box"]
ChatMessageType = Literal["text", "selection_box", "input_box"]
INPUT_BOX_MESSAGE_MAX_BYTES = 32_768
INPUT_BOX_MESSAGE_MAX_FIELDS = 100


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


class StrictChatContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatSyncRequest(StrictChatContractModel):
    request_id: str = Field(min_length=1)
    message_id: str = Field(
        min_length=1,
        description="Backend user chat_messages.public_id (opaque string).",
    )
    conversation_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    requested_return_type: RequestedReturnType | None
    message: str = Field(min_length=1)
    message_at: datetime

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_input_box_message(self) -> ChatSyncRequest:
        if self.requested_return_type != "input_box":
            return self
        parse_input_box_message(self.message)
        return self


class ChatTableRow(StrictChatContractModel):
    column: str = Field(min_length=1)
    value: str


class ChatTable(StrictChatContractModel):
    table_title: str | None = None
    rows: list[ChatTableRow] = Field(min_length=1)


class ChatInputOptions(StrictChatContractModel):
    unit: str | None = None
    lower: float | None = None
    upper: float | None = None
    selections: list[str] | None = None

    @field_validator("selections")
    @classmethod
    def validate_selections(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        normalized = [item.strip() for item in value]
        if any(not item for item in normalized):
            raise ValueError("input_option_selection_must_not_be_blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("input_option_selections_must_be_unique")
        return normalized


class ChatInput(StrictChatContractModel):
    type: Literal["number", "dropdown"]
    label: str = Field(min_length=1)
    value: str | float | None = None
    options: ChatInputOptions

    @model_validator(mode="after")
    def validate_options_for_type(self) -> ChatInput:
        if self.type == "number":
            if self.options.unit is None or self.options.lower is None or self.options.upper is None:
                raise ValueError("number_input_requires_unit_lower_and_upper")
            if self.options.lower > self.options.upper:
                raise ValueError("number_input_range_invalid")
            if self.options.selections is not None:
                raise ValueError("number_input_forbids_selections")
            if self.value is not None:
                try:
                    numeric_value = float(self.value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("number_input_value_must_be_numeric") from exc
                if not math.isfinite(numeric_value):
                    raise ValueError("number_input_value_must_be_finite")
                if numeric_value < self.options.lower or numeric_value > self.options.upper:
                    raise ValueError("number_input_value_out_of_range")
        else:
            if not self.options.selections:
                raise ValueError("dropdown_input_requires_selections")
            if any(
                value is not None
                for value in (self.options.unit, self.options.lower, self.options.upper)
            ):
                raise ValueError("dropdown_input_forbids_number_options")
            if self.value is not None and str(self.value) not in self.options.selections:
                raise ValueError("dropdown_input_value_must_be_one_of_selections")
        return self


class ChatMessageContent(StrictChatContractModel):
    message_title: str | None
    text: str | None
    tables: list[ChatTable] | None
    selections: list[str] | None
    inputs: list[ChatInput] | None


class ChatSyncResponse(StrictChatContractModel):
    request_id: str = Field(min_length=1)
    message_id: str = Field(
        min_length=1,
        description="Echo of the Backend user chat_messages.public_id.",
    )
    message_type: ChatMessageType
    message: ChatMessageContent
    message_at: datetime

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_message_type_payload(self) -> ChatSyncResponse:
        if self.message_type == "text":
            if self.message.selections is not None:
                raise ValueError("text_message_forbids_selections")
            if self.message.inputs is not None:
                raise ValueError("text_message_forbids_inputs")
            if not (
                _optional_text(self.message.text)
                or _optional_text(self.message.message_title)
                or self.message.tables
            ):
                raise ValueError("text_message_requires_display_content")
        elif self.message_type == "selection_box":
            if not self.message.selections:
                raise ValueError("selection_box_requires_selections")
            if self.message.inputs is not None:
                raise ValueError("selection_box_forbids_inputs")
            normalized = [selection.strip() for selection in self.message.selections]
            if any(not selection for selection in normalized):
                raise ValueError("selection_box_selection_must_not_be_blank")
            if len(set(normalized)) != len(normalized):
                raise ValueError("selection_box_selections_must_be_unique")
            self.message.selections = normalized
        else:
            if not self.message.inputs:
                raise ValueError("input_box_requires_inputs")
            if self.message.selections is not None:
                raise ValueError("input_box_forbids_selections")
        return self


class ChatContractError(StrictChatContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: dict[str, Any] | None = None


class ChatErrorResponse(StrictChatContractModel):
    error: ChatContractError


def agent_chat_payload(
    request: ChatSyncRequest,
    *,
    backend_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context = {
        "request_metadata": {
            "contract_version": "v1.2",
            "request_id": request.request_id,
            "message_id": request.message_id,
            "conversation_id": request.conversation_id,
            "patient_id": request.patient_id,
            "requested_return_type": request.requested_return_type,
            "message_at": request.message_at.isoformat(),
        }
    }
    if backend_context:
        context["backend_read_context"] = backend_context
    return {
        "patient_id": request.patient_id,
        "phr_patient_key": None,
        "event_type": "multiturn_chat",
        "message": request.message,
        "current_time": request.message_at,
        "context": context,
        "callback_context": None,
    }


def chat_sync_response(request: ChatSyncRequest, response: AgentResponse) -> ChatSyncResponse:
    message_type, message = _external_message(response)
    return ChatSyncResponse(
        request_id=request.request_id,
        message_id=request.message_id,
        message_type=message_type,
        message=message,
        message_at=datetime.now(UTC),
    )


def chat_error(
    code: str,
    message: str,
    *,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> ChatErrorResponse:
    return ChatErrorResponse(
        error=ChatContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        )
    )


def _external_message(response: AgentResponse) -> tuple[ChatMessageType, ChatMessageContent]:
    structured = response.structured_payload
    contract_message = _structured_contract_message(structured, response.human_summary)
    if contract_message is not None:
        return contract_message

    confirmation = structured.get("mutation_confirmation")
    if isinstance(confirmation, dict) and confirmation:
        display = confirmation.get("display") if isinstance(confirmation.get("display"), dict) else {}
        return (
            "selection_box",
            ChatMessageContent(
                message_title=_optional_text(display.get("title")),
                text=response.human_summary or _optional_text(display.get("question")),
                tables=None,
                selections=_confirmation_selections(display),
                inputs=None,
            ),
        )

    questionnaire = structured.get("ae_pro_ctcae")
    if isinstance(questionnaire, dict):
        question = _first_question(questionnaire)
        if question is not None:
            symptom_name = _optional_text(question.get("korean_symptom_name"))
            return (
                "selection_box",
                ChatMessageContent(
                    message_title=f"{symptom_name} 관련 자가 보고 설문" if symptom_name else "자가 보고 설문",
                    text=_optional_text(question.get("question")) or response.human_summary,
                    tables=None,
                    selections=[str(value) for value in question["response_options"]],
                    inputs=None,
                ),
            )

    selections = _candidate_selections(structured)
    if selections:
        return (
            "selection_box",
            ChatMessageContent(
                message_title="항목 선택",
                text=response.human_summary,
                tables=None,
                selections=selections,
                inputs=None,
            ),
        )

    return (
        "text",
        ChatMessageContent(
            message_title=None,
            text=response.human_summary,
            tables=None,
            selections=None,
            inputs=None,
        ),
    )


def parse_input_box_message(message: str) -> dict[str, str | int | float | bool | None]:
    """Parse the contract's JSON-object string without accepting ambiguous input."""

    if len(message.encode("utf-8")) > INPUT_BOX_MESSAGE_MAX_BYTES:
        raise ValueError("input_box_message_too_large")
    try:
        parsed = json.loads(
            message,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_non_finite_json_number,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("input_box_message_must_be_valid_json_object_string") from exc
    if not isinstance(parsed, dict):
        raise ValueError("input_box_message_must_be_json_object")
    if not parsed:
        raise ValueError("input_box_message_must_not_be_empty")
    if len(parsed) > INPUT_BOX_MESSAGE_MAX_FIELDS:
        raise ValueError("input_box_message_has_too_many_fields")
    for key, value in parsed.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("input_box_message_keys_must_not_be_blank")
        if isinstance(value, (dict, list)):
            raise ValueError("input_box_message_values_must_be_scalar")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("input_box_message_numbers_must_be_finite")
    return parsed


def input_box_message(
    labels: list[str],
    values: list[str],
) -> str:
    if not labels or len(labels) != len(values):
        raise ValueError("input_box_values_invalid")
    normalized_labels = [label.strip() for label in labels]
    if any(not label for label in normalized_labels):
        raise ValueError("input_box_labels_must_not_be_blank")
    if len(set(normalized_labels)) != len(normalized_labels):
        raise ValueError("input_box_labels_must_be_unique")
    encoded = json.dumps(
        dict(zip(normalized_labels, values, strict=True)),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    parse_input_box_message(encoded)
    return encoded


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("input_box_message_keys_must_be_unique")
        result[key] = value
    return result


def _reject_non_finite_json_number(value: str) -> None:
    raise ValueError(f"input_box_message_invalid_number:{value}")


def _structured_contract_message(
    structured: dict[str, Any],
    human_summary: str,
) -> tuple[ChatMessageType, ChatMessageContent] | None:
    candidate = _contract_message_candidate(structured)
    if candidate is None:
        return None
    message_type, raw_message = candidate
    inferred_type: ChatMessageType
    if message_type is not None and message_type not in {
        "text",
        "selection_box",
        "input_box",
    }:
        raise ValueError("structured_chat_message_type_invalid")
    if message_type in {"text", "selection_box", "input_box"}:
        inferred_type = message_type
    elif raw_message.get("inputs") is not None:
        inferred_type = "input_box"
    elif raw_message.get("selections") is not None:
        inferred_type = "selection_box"
    else:
        inferred_type = "text"
    content = ChatMessageContent.model_validate(
        {
            "message_title": raw_message.get("message_title"),
            "text": raw_message.get("text") or human_summary,
            "tables": raw_message.get("tables"),
            "selections": raw_message.get("selections"),
            "inputs": raw_message.get("inputs"),
        }
    )
    return inferred_type, content


def _contract_message_candidate(
    structured: dict[str, Any],
) -> tuple[str | None, dict[str, Any]] | None:
    for wrapper_key in ("chat_response", "chat_message", "external_message"):
        wrapper = structured.get(wrapper_key)
        if not isinstance(wrapper, dict):
            continue
        nested_message = wrapper.get("message")
        if isinstance(nested_message, dict):
            return _optional_text(wrapper.get("message_type")), nested_message
        if _has_contract_message_fields(wrapper):
            return _optional_text(wrapper.get("message_type")), wrapper

    for message_type in ("input_box", "selection_box"):
        raw_message = structured.get(message_type)
        if isinstance(raw_message, dict):
            return message_type, raw_message

    nested_message = structured.get("message")
    if isinstance(nested_message, dict) and _has_contract_message_fields(nested_message):
        return _optional_text(structured.get("message_type")), nested_message
    if _has_contract_message_fields(structured):
        return _optional_text(structured.get("message_type")), structured
    return None


def _has_contract_message_fields(value: dict[str, Any]) -> bool:
    return any(
        key in value
        for key in ("message_title", "text", "tables", "selections", "inputs")
    )


def _confirmation_selections(display: dict[str, Any]) -> list[str]:
    for key in ("selections", "options"):
        raw = display.get(key)
        if not isinstance(raw, list):
            continue
        values: list[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                values.append(item.strip())
            elif isinstance(item, dict):
                label = _optional_text(item.get("label") or item.get("text") or item.get("value"))
                if label:
                    values.append(label)
        if values:
            return values
    return ["변경 적용", "취소"]


def _first_question(questionnaire: dict[str, Any]) -> dict[str, Any] | None:
    questions = questionnaire.get("questions")
    if not isinstance(questions, list):
        return None
    for item in questions:
        if not isinstance(item, dict):
            continue
        options = item.get("response_options")
        if _optional_text(item.get("question")) and isinstance(options, list) and options:
            return item
    return None


def _candidate_selections(structured: dict[str, Any]) -> list[str]:
    raw_candidates = structured.get("food_candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raw_candidates = structured.get("diet_recommendations")
    if not isinstance(raw_candidates, list):
        return []
    values: list[str] = []
    for item in raw_candidates:
        if not isinstance(item, dict):
            continue
        label = _optional_text(item.get("food_name") or item.get("name") or item.get("title"))
        if label and label not in values:
            values.append(label)
    return values


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None
