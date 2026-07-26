from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictFeedbackContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatFeedbackRequest(StrictFeedbackContractModel):
    request_id: str = Field(min_length=1, max_length=160)
    message_id: str = Field(min_length=1, max_length=160)
    conversation_id: str = Field(min_length=1, max_length=160)
    patient_id: str = Field(min_length=1, max_length=160)
    feedback: bool
    feedback_text: str | None = Field(max_length=4_000)
    feedback_at: datetime

    @field_validator(
        "request_id",
        "message_id",
        "conversation_id",
        "patient_id",
    )
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier_must_not_be_blank")
        return normalized

    @field_validator("feedback_text")
    @classmethod
    def normalize_feedback_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("feedback_at")
    @classmethod
    def validate_feedback_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timezone_offset_required")
        return value


class ChatFeedbackAccepted(StrictFeedbackContractModel):
    status: Literal["accepted"] = "accepted"
