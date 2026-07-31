from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from shared.chat_contracts import ChatSyncRequest
from agent_app.persistence.models import AgentPatientLock, AgentSyncRequest
from shared.time_utils import utc_now

RECEIVED = "RECEIVED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
RETRYABLE_FAILED = "RETRYABLE_FAILED"
FINAL_FAILED = "FINAL_FAILED"


@dataclass(frozen=True)
class StoredHttpResponse:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class SyncRequestClaim:
    api_path: str
    request_id: str
    request_hash: str
    attempt_epoch: int


class SyncRequestGateError(RuntimeError):
    code = "SYNC_REQUEST_GATE_ERROR"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class IdempotencyConflictError(SyncRequestGateError):
    code = "IDEMPOTENCY_CONFLICT"


class RequestInProgressError(SyncRequestGateError):
    code = "REQUEST_IN_PROGRESS"
    retryable = True


class PatientThreadBusyError(SyncRequestGateError):
    code = "CONVERSATION_BUSY"
    retryable = True


class StaleSyncRequestAttemptError(RuntimeError):
    pass


def canonical_request_hash(request: ChatSyncRequest) -> str:
    body = request.model_dump(mode="json")
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SyncRequestGate:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        api_path: str,
        lock_lease_seconds: int,
        retention_seconds: int,
    ) -> None:
        self.session_factory = session_factory
        self.api_path = api_path
        self.lock_lease_seconds = max(1, lock_lease_seconds)
        self.retention_seconds = max(1, retention_seconds)

    def begin(
        self,
        request: ChatSyncRequest,
    ) -> StoredHttpResponse | SyncRequestClaim:
        request_hash = canonical_request_hash(request)
        for _ in range(3):
            with self.session_factory() as session:
                try:
                    return self._begin_once(session, request, request_hash)
                except IntegrityError:
                    session.rollback()
                    continue
                except Exception:
                    session.rollback()
                    raise
        raise RuntimeError("sync_request_gate_concurrent_insert_retry_exhausted")

    def complete(
        self,
        request: ChatSyncRequest,
        *,
        claim: SyncRequestClaim,
        status_code: int,
        body: dict[str, Any],
        trace_id: str,
    ) -> None:
        self._finish(
            request,
            status=COMPLETED,
            status_code=status_code,
            body=body,
            trace_id=trace_id,
            error_code="",
            claim=claim,
        )

    def bind_trace(
        self,
        request: ChatSyncRequest,
        trace_id: str,
        *,
        claim: SyncRequestClaim,
    ) -> None:
        now = utc_now()
        with self.session_factory() as session:
            record = session.scalar(
                select(AgentSyncRequest)
                .where(
                    AgentSyncRequest.api_path == self.api_path,
                    AgentSyncRequest.request_id == request.request_id,
                )
                .with_for_update()
            )
            self._assert_current_attempt(
                session,
                record,
                request,
                claim,
                operation="bind_trace",
            )
            record.trace_id = trace_id
            record.updated_at = now
            session.commit()

    def fail(
        self,
        request: ChatSyncRequest,
        *,
        claim: SyncRequestClaim,
        status_code: int,
        body: dict[str, Any],
        trace_id: str = "",
        error_code: str,
        retryable: bool,
    ) -> None:
        self._finish(
            request,
            status=RETRYABLE_FAILED if retryable else FINAL_FAILED,
            status_code=status_code,
            body=body,
            trace_id=trace_id,
            error_code=error_code,
            claim=claim,
        )

    def _begin_once(
        self,
        session: Session,
        request: ChatSyncRequest,
        request_hash: str,
    ) -> StoredHttpResponse | SyncRequestClaim:
        now = utc_now()
        lease_expires_at = now + timedelta(seconds=self.lock_lease_seconds)
        expires_at = now + timedelta(seconds=self.retention_seconds)
        record = session.scalar(
            select(AgentSyncRequest)
            .where(
                AgentSyncRequest.api_path == self.api_path,
                AgentSyncRequest.request_id == request.request_id,
            )
            .with_for_update()
        )

        if record is not None:
            if record.request_hash != request_hash:
                raise IdempotencyConflictError(
                    "The request_id was reused with a different request body.",
                    details={"request_id": request.request_id},
                )
            if record.status in {
                COMPLETED,
                FINAL_FAILED,
            }:
                return self._stored_response(record)
            if record.status in {RECEIVED, PROCESSING} and self._lease_is_active(
                record.lease_expires_at,
                now,
            ):
                raise RequestInProgressError(
                    "The same request_id is currently being processed.",
                    details={"request_id": request.request_id},
                )
            if record.status not in {RECEIVED, PROCESSING, RETRYABLE_FAILED}:
                raise RuntimeError(f"unsupported_sync_request_status:{record.status}")
        else:
            record = AgentSyncRequest(
                api_path=self.api_path,
                request_id=request.request_id,
                request_hash=request_hash,
                patient_id=request.patient_id,
                message_id=request.message_id,
                status=RECEIVED,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            session.add(record)
            session.flush()

        patient_lock = session.scalar(
            select(AgentPatientLock)
            .where(AgentPatientLock.patient_id == request.patient_id)
            .with_for_update()
        )
        if patient_lock is not None and self._lease_is_active(
            patient_lock.lease_expires_at,
            now,
        ):
            if patient_lock.request_id == request.request_id:
                raise RequestInProgressError(
                    "The same request_id is currently being processed.",
                    details={"request_id": request.request_id},
                )
            raise PatientThreadBusyError(
                "Another request is currently being processed for the conversation.",
                details={
                    "patient_id": request.patient_id,
                    "active_request_id": patient_lock.request_id,
                },
            )

        attempt_epoch = max(0, int(record.attempt_epoch or 0)) + 1
        if patient_lock is None:
            patient_lock = AgentPatientLock(
                patient_id=request.patient_id,
                request_id=request.request_id,
                attempt_epoch=attempt_epoch,
                lease_expires_at=lease_expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(patient_lock)
            session.flush()
        else:
            patient_lock.request_id = request.request_id
            patient_lock.attempt_epoch = attempt_epoch
            patient_lock.lease_expires_at = lease_expires_at
            patient_lock.updated_at = now

        record.status = PROCESSING
        record.attempt_epoch = attempt_epoch
        record.patient_id = request.patient_id
        record.message_id = request.message_id
        record.updated_at = now
        record.processing_started_at = now
        record.lease_expires_at = lease_expires_at
        record.completed_at = None
        record.expires_at = expires_at
        record.trace_id = ""
        record.response_status = None
        record.response_json = ""
        record.last_error_code = ""
        session.commit()
        return SyncRequestClaim(
            api_path=self.api_path,
            request_id=request.request_id,
            request_hash=request_hash,
            attempt_epoch=attempt_epoch,
        )

    def _finish(
        self,
        request: ChatSyncRequest,
        *,
        status: str,
        status_code: int,
        body: dict[str, Any],
        trace_id: str,
        error_code: str,
        claim: SyncRequestClaim,
    ) -> None:
        now = utc_now()
        with self.session_factory() as session:
            record = session.scalar(
                select(AgentSyncRequest)
                .where(
                    AgentSyncRequest.api_path == self.api_path,
                    AgentSyncRequest.request_id == request.request_id,
                )
                .with_for_update()
            )
            self._assert_current_attempt(
                session,
                record,
                request,
                claim,
                operation="finish",
            )
            record.status = status
            record.trace_id = trace_id
            record.response_status = status_code
            record.response_json = json.dumps(
                body,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            record.last_error_code = error_code
            record.updated_at = now
            record.lease_expires_at = None
            record.completed_at = now
            record.expires_at = now + timedelta(seconds=self.retention_seconds)
            session.execute(
                delete(AgentPatientLock).where(
                    AgentPatientLock.patient_id == request.patient_id,
                    AgentPatientLock.request_id == request.request_id,
                    AgentPatientLock.attempt_epoch
                    == claim.attempt_epoch,
                )
            )
            session.commit()

    def _assert_current_attempt(
        self,
        session: Session,
        record: AgentSyncRequest | None,
        request: ChatSyncRequest,
        claim: SyncRequestClaim,
        *,
        operation: str,
    ) -> None:
        request_hash = canonical_request_hash(request)
        if (
            claim.api_path != self.api_path
            or claim.request_id != request.request_id
            or claim.request_hash != request_hash
        ):
            raise RuntimeError("sync_request_claim_mismatch")
        if record is None:
            raise RuntimeError("sync_request_record_not_found")
        if record.request_hash != request_hash:
            raise RuntimeError("sync_request_hash_changed_while_processing")
        if record.attempt_epoch != claim.attempt_epoch:
            raise StaleSyncRequestAttemptError(
                f"stale_sync_request_attempt:{operation}"
            )
        if record.status != PROCESSING:
            raise RuntimeError("sync_request_not_processing")
        patient_lock = session.scalar(
            select(AgentPatientLock)
            .where(AgentPatientLock.patient_id == request.patient_id)
            .with_for_update()
        )
        if (
            patient_lock is None
            or patient_lock.request_id != request.request_id
            or patient_lock.attempt_epoch != claim.attempt_epoch
        ):
            raise StaleSyncRequestAttemptError(
                f"stale_sync_request_attempt:{operation}:patient_lock"
            )

    @staticmethod
    def _stored_response(record: AgentSyncRequest) -> StoredHttpResponse:
        if record.response_status is None or not record.response_json:
            raise RuntimeError("sync_request_stored_response_missing")
        try:
            body = json.loads(record.response_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("sync_request_stored_response_invalid") from exc
        if not isinstance(body, dict):
            raise RuntimeError("sync_request_stored_response_not_object")
        return StoredHttpResponse(status_code=record.response_status, body=body)

    @staticmethod
    def _lease_is_active(lease_expires_at: datetime | None, now: datetime) -> bool:
        return lease_expires_at is not None and lease_expires_at > now
