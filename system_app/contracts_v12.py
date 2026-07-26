from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.chat_contracts import ChatMessageContent, ChatMessageType, RequestedReturnType


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    return value


class StrictBackendContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackendChatRequest(StrictBackendContract):
    conversation_id: str = Field(min_length=1)
    patient_id: str = Field(min_length=1)
    message: str = Field(min_length=1)
    requested_return_type: RequestedReturnType | None = None
    request_id: str | None = None
    message_at: datetime | None = None

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime | None) -> datetime | None:
        return _aware(value) if value is not None else None


class BackendChatResponse(StrictBackendContract):
    request_id: str = Field(min_length=1)
    conversation_id: str = Field(min_length=1)
    user_message_id: str = Field(
        min_length=1,
        description="Persisted user chat_messages.public_id.",
    )
    assistant_message_id: str = Field(
        min_length=1,
        description="Persisted assistant chat_messages.public_id.",
    )
    message_type: ChatMessageType
    message: ChatMessageContent
    message_at: datetime

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime) -> datetime:
        return _aware(value)


class BackendChatFailure(StrictBackendContract):
    request_id: str
    conversation_id: str
    user_message_id: str
    status: Literal["failed"] = "failed"
    error_code: str
    retryable: bool
