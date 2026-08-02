from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from shared.feedback_contracts import ChatFeedbackAccepted, ChatFeedbackRequest
from shared.public_ids import RequestId

__all__ = [
    "CallbackListResponse",
    "CallbackReleaseResponse",
    "CallbackView",
    "ChatFeedbackAccepted",
    "ChatFeedbackRequest",
    "HealthResponse",
    "ReadinessResponse",
    "StrictContractModel",
]


class StrictContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CallbackView(StrictContractModel):
    callback_request_id: RequestId
    source_request_id: RequestId
    callback_kind: Literal["missed_dose_result", "notification_policy_proposal"]
    callback_path: str
    status: Literal[
        "held",
        "pending",
        "in_progress",
        "retry_wait",
        "completed",
        "dead",
    ]
    attempts: int
    next_attempt_at: datetime | None
    last_http_status: int | None
    last_error_code: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class CallbackListResponse(StrictContractModel):
    callbacks: list[CallbackView]


class CallbackReleaseResponse(StrictContractModel):
    callback_request_id: RequestId
    status: Literal["pending", "already_released"]


class HealthResponse(StrictContractModel):
    status: Literal["ok"]
    service: Literal["dranswer-agent-contract-test-server"]
    release: str


class ReadinessResponse(StrictContractModel):
    status: Literal["ready", "not_ready"]
    service: Literal["dranswer-agent-contract-test-server"]
    release: str
    database_ready: bool
    callback_mode: Literal["hold", "deliver"]
    callback_delivery_ready: bool
    errors: list[str]
