from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.chat_contracts import ChatSyncRequest
from agent_app.persistence.models import AgentConversationLock, AgentSyncRequest
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


class ConversationBusyError(SyncRequestGateError):
    code = "CONVERSATION_BUSY"
    retryable = True


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

    def begin(self, request: ChatSyncRequest) -> StoredHttpResponse | None:
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
        )

    def bind_trace(self, request: ChatSyncRequest, trace_id: str) -> None:
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
            if record is None or record.status != PROCESSING:
                raise RuntimeError("sync_request_not_processing")
            if record.request_hash != canonical_request_hash(request):
                raise RuntimeError("sync_request_hash_changed_before_trace_binding")
            record.trace_id = trace_id
            record.updated_at = now
            session.commit()

    def fail(
        self,
        request: ChatSyncRequest,
        *,
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
        )

    def purge_expired(self, *, now: datetime | None = None) -> tuple[int, int]:
        current = now or utc_now()
        with self.session_factory() as session:
            deleted_locks = session.execute(
                delete(AgentConversationLock).where(AgentConversationLock.lease_expires_at <= current)
            ).rowcount
            deleted_requests = session.execute(
                delete(AgentSyncRequest).where(
                    AgentSyncRequest.expires_at <= current,
                    AgentSyncRequest.status.in_((COMPLETED, RETRYABLE_FAILED, FINAL_FAILED)),
                )
            ).rowcount
            session.commit()
        return int(deleted_requests or 0), int(deleted_locks or 0)

    def _begin_once(
        self,
        session: Session,
        request: ChatSyncRequest,
        request_hash: str,
    ) -> StoredHttpResponse | None:
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
            if record.status in {COMPLETED, FINAL_FAILED}:
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
                conversation_id=request.conversation_id,
                patient_id=request.patient_id,
                message_id=request.message_id,
                status=RECEIVED,
                created_at=now,
                updated_at=now,
                expires_at=expires_at,
            )
            session.add(record)
            session.flush()

        conversation_lock = session.scalar(
            select(AgentConversationLock)
            .where(AgentConversationLock.conversation_id == request.conversation_id)
            .with_for_update()
        )
        if conversation_lock is not None and self._lease_is_active(
            conversation_lock.lease_expires_at,
            now,
        ):
            if conversation_lock.request_id == request.request_id:
                raise RequestInProgressError(
                    "The same request_id is currently being processed.",
                    details={"request_id": request.request_id},
                )
            raise ConversationBusyError(
                "Another request is currently being processed for the conversation.",
                details={
                    "conversation_id": request.conversation_id,
                    "active_request_id": conversation_lock.request_id,
                },
            )

        if conversation_lock is None:
            conversation_lock = AgentConversationLock(
                conversation_id=request.conversation_id,
                request_id=request.request_id,
                lease_expires_at=lease_expires_at,
                created_at=now,
                updated_at=now,
            )
            session.add(conversation_lock)
            session.flush()
        else:
            conversation_lock.request_id = request.request_id
            conversation_lock.lease_expires_at = lease_expires_at
            conversation_lock.updated_at = now

        record.status = PROCESSING
        record.conversation_id = request.conversation_id
        record.patient_id = request.patient_id
        record.message_id = request.message_id
        record.updated_at = now
        record.processing_started_at = now
        record.lease_expires_at = lease_expires_at
        record.completed_at = None
        record.expires_at = expires_at
        record.response_status = None
        record.response_json = ""
        record.last_error_code = ""
        session.commit()
        return None

    def _finish(
        self,
        request: ChatSyncRequest,
        *,
        status: str,
        status_code: int,
        body: dict[str, Any],
        trace_id: str,
        error_code: str,
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
            if record is None:
                raise RuntimeError("sync_request_record_not_found")
            if record.request_hash != canonical_request_hash(request):
                raise RuntimeError("sync_request_hash_changed_while_processing")
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
                delete(AgentConversationLock).where(
                    AgentConversationLock.conversation_id == request.conversation_id,
                    AgentConversationLock.request_id == request.request_id,
                )
            )
            session.commit()

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
