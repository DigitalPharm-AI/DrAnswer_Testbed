from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas import AgentResponse

RequestedReturnType = Literal["selection_box", "input_box"]
ChatMessageType = Literal["text", "selection_box", "input_box"]


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


class StrictChatContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatSyncRequest(StrictChatContractModel):
    request_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
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
        try:
            parsed = json.loads(self.message)
        except json.JSONDecodeError as exc:
            raise ValueError("input_box_message_must_be_valid_json_object_string") from exc
        if not isinstance(parsed, dict):
            raise ValueError("input_box_message_must_be_json_object")
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
        elif not self.options.selections:
            raise ValueError("dropdown_input_requires_selections")
        return self


class ChatMessageContent(StrictChatContractModel):
    message_title: str | None
    text: str | None
    tables: list[ChatTable] | None
    selections: list[str] | None
    inputs: list[ChatInput] | None


class ChatSyncResponse(StrictChatContractModel):
    request_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    message_type: ChatMessageType
    message: ChatMessageContent
    message_at: datetime

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_message_type_payload(self) -> ChatSyncResponse:
        if self.message_type == "selection_box" and not self.message.selections:
            raise ValueError("selection_box_requires_selections")
        if self.message_type == "input_box" and not self.message.inputs:
            raise ValueError("input_box_requires_inputs")
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
