from __future__ import annotations

from datetime import datetime

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from shared.chat_contracts import (
    ChatMessageContent,
    ChatMessageType,
    RequestedReturnType,
    require_structured_response_source,
)
from shared.public_ids import (
    AssistantMessageId,
    PatientId,
    RequestId,
    UserMessageId,
)
from shared.time_utils import (
    require_aware_datetime,
    require_optional_aware_datetime,
)


class StrictBackendContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BackendChatRequest(StrictBackendContract):
    patient_id: PatientId
    message: str = Field(min_length=1)
    requested_return_type: RequestedReturnType
    source_message_id: AssistantMessageId | None = Field(
        default=None,
        min_length=1,
        max_length=180,
        description="Assistant message being answered by a structured reply.",
    )
    request_id: RequestId | None = None
    message_at: datetime | None = None

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime | None) -> datetime | None:
        return require_optional_aware_datetime(value)

    @field_validator("source_message_id")
    @classmethod
    def normalize_source_message_id(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("blank_source_message_id_not_allowed")
        return normalized

    @model_validator(mode="after")
    def validate_structured_response_source(self) -> BackendChatRequest:
        require_structured_response_source(
            self.requested_return_type,
            self.source_message_id,
        )
        return self


class BackendChatResponse(StrictBackendContract):
    request_id: RequestId
    user_message_id: UserMessageId = Field(
        description="Persisted user chat_messages.public_id.",
    )
    assistant_message_id: AssistantMessageId = Field(
        description="Persisted assistant chat_messages.public_id.",
    )
    message_type: ChatMessageType
    message: ChatMessageContent
    message_at: datetime

    @field_validator("message_at")
    @classmethod
    def validate_message_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)
