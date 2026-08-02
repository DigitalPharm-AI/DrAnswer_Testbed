from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from shared.public_ids import AssistantMessageId, PatientId, RequestId
from shared.time_utils import (
    require_aware_datetime,
    require_optional_aware_datetime,
)


class StrictFeedbackContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatFeedbackRequest(StrictFeedbackContractModel):
    request_id: RequestId
    message_id: AssistantMessageId
    patient_id: PatientId
    reaction: Literal["like", "dislike"] | None = None
    feedback_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=4_000,
    )
    feedback_at: datetime

    @field_validator("feedback_text")
    @classmethod
    def normalize_feedback_text(
        cls,
        value: str | None,
    ) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("feedback_text_must_not_be_blank")
        return normalized

    @field_validator("feedback_at")
    @classmethod
    def validate_feedback_at(cls, value: datetime) -> datetime:
        return require_aware_datetime(value)

    @model_validator(mode="after")
    def require_reaction_or_opinion(self) -> ChatFeedbackRequest:
        if self.reaction is None and self.feedback_text is None:
            raise ValueError("reaction_or_feedback_text_required")
        return self


class ChatFeedbackAccepted(StrictFeedbackContractModel):
    status: Literal["accepted"]
    reaction: Literal["like", "dislike"] | None = None
    accepted_at: datetime | None = None

    @field_validator("accepted_at")
    @classmethod
    def validate_accepted_at(
        cls,
        value: datetime | None,
    ) -> datetime | None:
        return require_optional_aware_datetime(value)
