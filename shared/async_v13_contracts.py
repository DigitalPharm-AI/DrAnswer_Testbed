from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.backend_v13_contracts import NotificationPolicyChanges
from shared.public_ids import DoseEventId, PatientId, RequestId


class StrictAsyncV13Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MissedDoseEventRequest(StrictAsyncV13Model):
    request_id: RequestId
    patient_id: PatientId
    dose_event_id: DoseEventId


class DailyMedicationPatternAnalysisRequest(StrictAsyncV13Model):
    request_id: RequestId
    patient_id: list[PatientId] = Field(min_length=1)
    analysis_date: date

    @model_validator(mode="after")
    def validate_unique_patients(
        self,
    ) -> DailyMedicationPatternAnalysisRequest:
        if len(set(self.patient_id)) != len(self.patient_id):
            raise ValueError(
                "daily_pattern_patient_id_must_be_unique"
            )
        return self


class AsyncEventAccepted(StrictAsyncV13Model):
    request_id: RequestId
    status: Literal["accepted", "duplicate"]


class MissedDoseResult(StrictAsyncV13Model):
    message: str = Field(min_length=1)
    requires_reply: bool


class AsyncProcessingError(StrictAsyncV13Model):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool


class MissedDoseResultCallback(StrictAsyncV13Model):
    request_id: RequestId
    status: Literal["completed", "failed"]
    result: MissedDoseResult | None
    error: AsyncProcessingError | None

    @model_validator(mode="after")
    def validate_outcome(self) -> MissedDoseResultCallback:
        if self.status == "completed":
            if self.result is None or self.error is not None:
                raise ValueError(
                    "completed_callback_requires_result_without_error"
                )
        elif self.result is not None or self.error is None:
            raise ValueError(
                "failed_callback_requires_error_without_result"
            )
        return self


class AsyncResultCallbackAck(StrictAsyncV13Model):
    request_id: RequestId
    status: Literal["processed", "duplicate"]


class NotificationPolicyProposal(NotificationPolicyChanges):
    """The proposal contract reuses the synchronous policy limits."""


class NotificationPolicyChangeProposalRequest(StrictAsyncV13Model):
    request_id: RequestId
    patient_id: PatientId
    proposed_policy: NotificationPolicyProposal
    reason: str = Field(min_length=1)


class NotificationPolicyProposalAck(StrictAsyncV13Model):
    request_id: RequestId
    status: Literal["accepted", "duplicate"]
