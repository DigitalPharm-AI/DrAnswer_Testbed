from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_app.persistence.models import AgentBackendWriteRequest
from shared.json_utils import dump_json, parse_json_object, sha256_json
from shared.time_utils import utc_now

PREPARED = "PREPARED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
RETRYABLE_FAILED = "RETRYABLE_FAILED"


class BackendWriteStateConflict(RuntimeError):
    """The same Backend request_id was presented with different trusted input."""


@dataclass(frozen=True)
class BackendWriteIdentity:
    request_id: str
    source_chat_request_id: str
    patient_id_hash: str
    trusted_context_hash: str
    tool_call_id: str
    tool_name: str
    argument_hash: str


@dataclass(frozen=True)
class BackendWriteState:
    request_id: str
    tool_call_id: str
    expected_version: int | None
    status: str
    response_status: int | None
    response_body: dict[str, Any] | None


def canonical_payload_hash(value: Any) -> str:
    return sha256_json(value)


class BackendWriteStateStore:
    """AI-DB source of truth for restart-safe Backend write retries.

    Only hashes, the optimistic-lock version, and the already validated Backend
    response are stored. Raw patient context and model arguments are not stored.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        retention_seconds: int,
    ) -> None:
        self.session_factory = session_factory
        self.retention_seconds = max(1, int(retention_seconds))

    def load(
        self,
        identity: BackendWriteIdentity,
    ) -> BackendWriteState | None:
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentBackendWriteRequest).where(
                    AgentBackendWriteRequest.request_id == identity.request_id
                )
            )
            if row is None:
                return None
            self._validate_identity(row, identity)
            return self._state(row)

    def find_equivalent(
        self,
        identity: BackendWriteIdentity,
    ) -> BackendWriteState | None:
        """Find a logical retry whose LLM-generated tool_call_id changed."""

        with self.session_factory() as session:
            row = session.scalar(
                select(AgentBackendWriteRequest).where(
                    AgentBackendWriteRequest.source_chat_request_id
                    == identity.source_chat_request_id,
                    AgentBackendWriteRequest.tool_name == identity.tool_name,
                    AgentBackendWriteRequest.argument_hash
                    == identity.argument_hash,
                    AgentBackendWriteRequest.trusted_context_hash
                    == identity.trusted_context_hash,
                )
            )
            if row is None:
                return None
            self._validate_logical_identity(row, identity)
            return self._state(row)

    def prepare(
        self,
        identity: BackendWriteIdentity,
        *,
        expected_version: int | None,
    ) -> BackendWriteState:
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentBackendWriteRequest).where(
                    AgentBackendWriteRequest.request_id == identity.request_id
                )
            )
            if row is not None:
                self._validate_identity(row, identity)
                return self._state(row)

            row = AgentBackendWriteRequest(
                request_id=identity.request_id,
                source_chat_request_id=identity.source_chat_request_id,
                patient_id_hash=identity.patient_id_hash,
                trusted_context_hash=identity.trusted_context_hash,
                tool_call_id=identity.tool_call_id,
                tool_name=identity.tool_name,
                argument_hash=identity.argument_hash,
                expected_version=expected_version,
                status=PREPARED,
                created_at=now,
                updated_at=now,
                expires_at=now + timedelta(seconds=self.retention_seconds),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Another AI process prepared the same deterministic request.
                session.rollback()
                row = session.scalar(
                    select(AgentBackendWriteRequest).where(
                        AgentBackendWriteRequest.request_id == identity.request_id
                    )
                )
                if row is None:
                    row = session.scalar(
                        select(AgentBackendWriteRequest).where(
                            AgentBackendWriteRequest.source_chat_request_id
                            == identity.source_chat_request_id,
                            AgentBackendWriteRequest.tool_name
                            == identity.tool_name,
                            AgentBackendWriteRequest.argument_hash
                            == identity.argument_hash,
                            AgentBackendWriteRequest.trusted_context_hash
                            == identity.trusted_context_hash,
                        )
                    )
                if row is None:
                    raise
                self._validate_logical_identity(row, identity)
                return self._state(row)
            session.refresh(row)
            return self._state(row)

    def begin_attempt(self, request_id: str) -> BackendWriteState:
        now = utc_now()
        with self.session_factory() as session:
            session.execute(
                update(AgentBackendWriteRequest)
                .where(
                    AgentBackendWriteRequest.request_id == request_id,
                    AgentBackendWriteRequest.status != COMPLETED,
                )
                .values(
                    status=PROCESSING,
                    attempt_count=AgentBackendWriteRequest.attempt_count + 1,
                    last_attempt_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            row = self._required_row(session, request_id)
            return self._state(row)

    def record_response(
        self,
        request_id: str,
        *,
        status_code: int,
        body: dict[str, Any],
        terminal: bool,
        error_code: str = "",
    ) -> BackendWriteState:
        now = utc_now()
        with self.session_factory() as session:
            serialized = dump_json(body)
            values: dict[str, Any] = {
                "response_status": status_code,
                "response_hash": canonical_payload_hash(body),
                "error_code": error_code,
                "updated_at": now,
            }
            if terminal:
                values.update(
                    {
                        "status": COMPLETED,
                        "response_json": serialized,
                        "completed_at": now,
                    }
                )
            else:
                values.update(
                    {
                        "status": RETRYABLE_FAILED,
                        "response_json": "",
                        "completed_at": None,
                    }
                )
            # The predicate is evaluated by the database while it owns the
            # write lock. A concurrent terminal result therefore becomes
            # immutable before any late retryable response can update it.
            session.execute(
                update(AgentBackendWriteRequest)
                .where(
                    AgentBackendWriteRequest.request_id == request_id,
                    AgentBackendWriteRequest.status != COMPLETED,
                )
                .values(**values)
            )
            session.commit()
            row = self._required_row(session, request_id)
            return self._state(row)

    def record_transport_failure(
        self,
        request_id: str,
        *,
        error_code: str,
    ) -> BackendWriteState:
        now = utc_now()
        with self.session_factory() as session:
            session.execute(
                update(AgentBackendWriteRequest)
                .where(
                    AgentBackendWriteRequest.request_id == request_id,
                    AgentBackendWriteRequest.status != COMPLETED,
                )
                .values(
                    status=RETRYABLE_FAILED,
                    error_code=error_code[:80],
                    updated_at=now,
                    completed_at=None,
                )
            )
            session.commit()
            row = self._required_row(session, request_id)
            return self._state(row)

    @staticmethod
    def _required_row(
        session: Session,
        request_id: str,
    ) -> AgentBackendWriteRequest:
        row = session.scalar(
            select(AgentBackendWriteRequest).where(
                AgentBackendWriteRequest.request_id == request_id
            )
        )
        if row is None:
            raise RuntimeError("backend_write_state_not_prepared")
        return row

    @staticmethod
    def _validate_identity(
        row: AgentBackendWriteRequest,
        identity: BackendWriteIdentity,
    ) -> None:
        actual = (
            row.source_chat_request_id,
            row.patient_id_hash,
            row.trusted_context_hash,
            row.tool_name,
            row.argument_hash,
        )
        expected = (
            identity.source_chat_request_id,
            identity.patient_id_hash,
            identity.trusted_context_hash,
            identity.tool_name,
            identity.argument_hash,
        )
        if actual != expected:
            raise BackendWriteStateConflict(
                "backend_write_request_id_reused_with_different_context"
            )

    @staticmethod
    def _validate_logical_identity(
        row: AgentBackendWriteRequest,
        identity: BackendWriteIdentity,
    ) -> None:
        actual = (
            row.source_chat_request_id,
            row.patient_id_hash,
            row.trusted_context_hash,
            row.tool_name,
            row.argument_hash,
        )
        expected = (
            identity.source_chat_request_id,
            identity.patient_id_hash,
            identity.trusted_context_hash,
            identity.tool_name,
            identity.argument_hash,
        )
        if actual != expected:
            raise BackendWriteStateConflict(
                "backend_write_logical_call_reused_with_different_context"
            )

    @staticmethod
    def _state(row: AgentBackendWriteRequest) -> BackendWriteState:
        response_body = (
            parse_json_object(row.response_json)
            if row.status == COMPLETED and row.response_json
            else None
        )
        return BackendWriteState(
            request_id=row.request_id,
            tool_call_id=row.tool_call_id,
            expected_version=row.expected_version,
            status=row.status,
            response_status=row.response_status,
            response_body=response_body,
        )
