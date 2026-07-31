from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.contract_boundary import remove_retired_conversation_fields
from shared.public_ids import PatientId, RequestId, UserMessageId
from shared.schemas import AgentResponse

RequestedReturnType = Literal["text", "selection_box", "input_box"]
ChatMessageType = Literal["text", "selection_box", "input_box"]
ChatStreamStatus = Literal["streaming", "completed", "error"]
INPUT_BOX_MESSAGE_MAX_BYTES = 32_768
INPUT_BOX_MESSAGE_MAX_FIELDS = 100


def _validate_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


class StrictChatContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatSyncRequest(StrictChatContractModel):
    request_id: RequestId
    message_id: UserMessageId = Field(
        description="Backend user chat_messages.public_id (opaque string).",
    )
    patient_id: PatientId
    requested_return_type: RequestedReturnType
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
    request_id: RequestId
    message_id: UserMessageId = Field(
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
    details: dict[str, Any] | None


class ChatErrorResponse(StrictChatContractModel):
    request_id: RequestId | None
    error: ChatContractError


class ChatStreamEvent(StrictChatContractModel):
    request_id: RequestId
    message_id: UserMessageId
    sequence: int = Field(ge=0)
    status: ChatStreamStatus
    message_type: ChatMessageType | None
    delta: str | None
    message: ChatMessageContent | None
    error: ChatContractError | None
    event_at: datetime

    @field_validator("event_at")
    @classmethod
    def validate_event_at(cls, value: datetime) -> datetime:
        return _validate_aware_datetime(value)

    @model_validator(mode="after")
    def validate_status_payload(self) -> ChatStreamEvent:
        if self.status == "streaming":
            if self.message_type != "text":
                raise ValueError("streaming_event_requires_text_message_type")
            if self.delta is None or not self.delta:
                raise ValueError("streaming_event_requires_delta")
            if self.message is not None or self.error is not None:
                raise ValueError("streaming_event_forbids_message_and_error")
            return self

        if self.status == "completed":
            if self.message_type is None or self.message is None:
                raise ValueError(
                    "completed_event_requires_message_type_and_message"
                )
            if self.delta is not None or self.error is not None:
                raise ValueError("completed_event_forbids_delta_and_error")
            ChatSyncResponse(
                request_id=self.request_id,
                message_id=self.message_id,
                message_type=self.message_type,
                message=self.message,
                message_at=self.event_at,
            )
            return self

        if (
            self.message_type is not None
            or self.delta is not None
            or self.message is not None
            or self.error is None
        ):
            raise ValueError(
                "error_event_requires_only_error_payload"
            )
        return self


def agent_chat_payload(
    request: ChatSyncRequest,
    *,
    backend_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    backend_context = (
        remove_retired_conversation_fields(backend_context)
        if backend_context is not None
        else None
    )
    context = {
        "request_metadata": {
            "contract_version": "v1.3",
            "request_id": request.request_id,
            "message_id": request.message_id,
            "patient_id": request.patient_id,
            "requested_return_type": request.requested_return_type,
            "message_at": request.message_at.isoformat(),
        }
    }
    if backend_context:
        context["backend_read_context"] = backend_context
        recent_chat = backend_context.get("recent_chat")
        if isinstance(recent_chat, list):
            context["recent_chat"] = recent_chat
            context["recent_chat_complete"] = (
                backend_context.get("recent_chat_complete") is True
            )
            context["recent_chat_limit"] = backend_context.get(
                "recent_chat_limit"
            )
        structured_response_context = backend_context.get(
            "structured_response_context"
        )
        if isinstance(structured_response_context, dict):
            context["structured_response_context"] = (
                structured_response_context
            )
        patient_snapshot = backend_context.get("patient_context_snapshot")
        if isinstance(patient_snapshot, dict):
            context["trusted_patient_context"] = patient_snapshot
            context["patient_context_snapshot"] = llm_safe_patient_snapshot(
                patient_snapshot
            )
        missed_dose_reply = backend_context.get("missed_dose_reply")
        if isinstance(missed_dose_reply, dict):
            context["missed_dose_reply"] = missed_dose_reply
    return {
        "patient_id": request.patient_id,
        "event_type": "multiturn_chat",
        "message": request.message,
        "current_time": request.message_at,
        "context": context,
        "callback_context": None,
    }


def llm_safe_patient_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    safe = {
        "as_of": snapshot.get("as_of"),
        "date": snapshot.get("date"),
        "read_contract_version": snapshot.get("read_contract_version"),
        "context_mode": snapshot.get("context_mode"),
        "availability": snapshot.get("availability"),
        "profile": snapshot.get("profile"),
        "active_conditions": snapshot.get("active_conditions"),
        "active_treatments": snapshot.get("active_treatments"),
        "active_medication_schedules": snapshot.get(
            "active_medication_schedules"
        ),
        "today_medication": snapshot.get("today_medication"),
        "today_meals": snapshot.get("today_meals"),
        "active_notification_policies": snapshot.get(
            "active_notification_policies"
        ),
    }
    return _remove_tool_managed_fields(
        remove_retired_conversation_fields(safe)
    )


def _remove_tool_managed_fields(value: Any) -> Any:
    hidden_fields = {
        "conversation_id",
        "conversationId",
        "patient_id",
        "dose_event_id",
        "policy_id",
        "record_id",
        "parent_record_id",
        "version",
    }
    if isinstance(value, dict):
        return {
            key: _remove_tool_managed_fields(item)
            for key, item in value.items()
            if key not in hidden_fields
        }
    if isinstance(value, list):
        return [_remove_tool_managed_fields(item) for item in value]
    return value


def chat_sync_response(request: ChatSyncRequest, response: AgentResponse) -> ChatSyncResponse:
    message_type, message = _external_message(response)
    return ChatSyncResponse(
        request_id=request.request_id,
        message_id=request.message_id,
        message_type=message_type,
        message=message,
        message_at=datetime.now(UTC),
    )


def has_structured_ui_response(response: AgentResponse) -> bool:
    """Return whether the completed Agent response already owns a UI card."""

    structured = response.structured_payload
    candidate = _contract_message_candidate(structured)
    if candidate is not None:
        declared_type, raw_message = candidate
        return (
            declared_type in {"selection_box", "input_box"}
            or raw_message.get("selections") is not None
            or raw_message.get("inputs") is not None
        )
    if isinstance(structured.get("mutation_confirmation"), dict):
        return True
    questionnaire = structured.get("ae_pro_ctcae")
    if isinstance(questionnaire, dict) and _first_question(questionnaire) is not None:
        return True
    return bool(_candidate_selections(structured))


def chat_error(
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    retryable: bool,
    details: dict[str, Any] | None = None,
) -> ChatErrorResponse:
    return ChatErrorResponse(
        request_id=request_id,
        error=ChatContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=remove_retired_conversation_fields(details),
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
                tables=_confirmation_tables(display),
                selections=_confirmation_selections(display),
                inputs=None,
            ),
        )

    questionnaire = structured.get("ae_pro_ctcae")
    if isinstance(questionnaire, dict):
        question = _first_question(questionnaire)
        if question is None:
            raise ValueError("ae_pro_ctcae_question_invalid")
        symptom_name = (
            _optional_text(question.get("korean_symptom_name"))
            or _optional_text(questionnaire.get("matched_korean_symptom_name"))
            or _optional_text(questionnaire.get("input_symptom"))
        )
        if symptom_name is None:
            raise ValueError("ae_pro_ctcae_symptom_name_missing")
        question_text = _optional_text(question.get("question"))
        if question_text is None:
            raise ValueError("ae_pro_ctcae_question_text_missing")
        symptom_subject = (
            symptom_name if symptom_name.endswith("증상") else f"{symptom_name} 증상"
        )
        guidance = (
            f"{symptom_subject}이 약물과 관련이 있을 수 있습니다. "
            "아래의 질문에 답변해 주시면 증상을 더 정확하게 평가할 수 있습니다."
        )
        return (
            "selection_box",
            ChatMessageContent(
                message_title=f"{symptom_name} 관련 자가 보고 설문",
                text=f"{guidance}\n\n{question_text}",
                tables=None,
                selections=[str(value) for value in question["response_options"]],
                inputs=None,
            ),
        )

    selections = _candidate_selections(structured)
    if selections:
        progress = structured.get(
            "food_selection_progress"
        )
        title = "항목 선택"
        if isinstance(progress, dict):
            current_group = progress.get("current_group")
            total_groups = progress.get("total_groups")
            if (
                isinstance(current_group, int)
                and isinstance(total_groups, int)
                and total_groups > 1
                and 1 <= current_group <= total_groups
            ):
                title = (
                    f"항목 선택 ({current_group}/"
                    f"{total_groups})"
                )
        return (
            "selection_box",
            ChatMessageContent(
                message_title=title,
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
    return [
        _optional_text(display.get("action_label")) or "변경 적용",
        "취소",
    ]


def _confirmation_tables(
    display: dict[str, Any],
) -> list[ChatTable] | None:
    raw_tables = display.get("tables")
    if raw_tables is None:
        return None
    if not isinstance(raw_tables, list):
        raise ValueError("mutation_confirmation_tables_invalid")
    tables = [
        ChatTable.model_validate(table)
        for table in raw_tables
        if isinstance(table, dict)
    ]
    if len(tables) != len(raw_tables):
        raise ValueError("mutation_confirmation_tables_invalid")
    return tables or None


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
