from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from shared.contract_boundary import remove_retired_conversation_fields
from shared.public_ids import AssistantMessageId, RequestId


class StrictUiFeedbackContract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UiChatOpinionRequest(StrictUiFeedbackContract):
    request_id: RequestId
    assistant_message_id: AssistantMessageId
    reaction: Literal["like", "dislike"] | None = None
    opinion_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
    )
    feedback_at: datetime

    @field_validator("opinion_text")
    @classmethod
    def normalize_opinion_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("opinion_text_must_not_be_blank")
        return normalized

    @field_validator("feedback_at")
    @classmethod
    def validate_feedback_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_offset_required")
        return value

    @model_validator(mode="after")
    def require_reaction_or_opinion(self) -> UiChatOpinionRequest:
        if self.reaction is None and self.opinion_text is None:
            raise ValueError("reaction_or_opinion_text_required")
        return self


class UiChatOpinionAccepted(StrictUiFeedbackContract):
    status: Literal["accepted"]
    request_id: RequestId
    assistant_message_id: AssistantMessageId
    reaction: Literal["like", "dislike"] | None
    opinion_submitted: bool
    opinion_submitted_at: datetime | None


class AgentChatFeedbackAccepted(StrictUiFeedbackContract):
    status: Literal["accepted"]
    reaction: Literal["like", "dislike"] | None = None
    accepted_at: datetime | None = None

    @field_validator("accepted_at")
    @classmethod
    def validate_accepted_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        if value is not None and (
            value.tzinfo is None or value.utcoffset() is None
        ):
            raise ValueError("timezone_offset_required")
        return value


class UiChatOpinionAcceptedResponse(StrictUiFeedbackContract):
    success: Literal[True]
    data: UiChatOpinionAccepted
    error: None


class UiFeedbackError(StrictUiFeedbackContract):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any] | None

    @field_validator("details")
    @classmethod
    def remove_retired_identifiers(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        return remove_retired_conversation_fields(value)


class UiFeedbackErrorResponse(StrictUiFeedbackContract):
    success: Literal[False]
    data: None
    error: UiFeedbackError
