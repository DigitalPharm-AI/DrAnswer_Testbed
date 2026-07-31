from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.feedback_contracts import (
    ChatFeedbackAccepted,
    ChatFeedbackRequest,
)
from agent_app.integration.feedback_crypto import (
    FeedbackCipher,
    FeedbackEncryptionContext,
)
from agent_app.observability.outbox import enqueue_user_feedback_score
from agent_app.persistence.models import AgentFeedbackLink
from shared.settings import Settings
from shared.time_utils import utc_now

FEEDBACK_API_PATH = "/agent/async/chat_feedback"
ACCEPTED = "ACCEPTED"
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
RETRYABLE_FAILED = "RETRYABLE_FAILED"
FINAL_FAILED = "FINAL_FAILED"


class FeedbackIdempotencyConflict(RuntimeError):
    pass


class FeedbackTargetMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackHttpResponse:
    status_code: int
    body: dict[str, Any]


@dataclass(frozen=True)
class FeedbackWorkItem:
    record_id: int
    request_id: str
    attempt_count: int
    max_attempts: int


class ChatFeedbackService:
    """Durable ingress and retry state for encrypted chat feedback."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        cipher: FeedbackCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = cipher or FeedbackCipher.from_settings(settings)
        self.retention_seconds = max(
            1,
            int(settings.agent_feedback_retention_seconds),
        )
        self.max_attempts = max(
            1,
            int(settings.agent_feedback_max_attempts),
        )
        self.processing_lease_seconds = max(
            1,
            int(settings.agent_feedback_processing_lease_seconds),
        )
        self.retry_base_seconds = max(
            1,
            int(settings.agent_feedback_retry_base_seconds),
        )
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            int(settings.agent_feedback_retry_max_seconds),
        )

    def replay(
        self,
        payload: ChatFeedbackRequest,
    ) -> FeedbackHttpResponse | None:
        request_hash = self.cipher.request_digest(payload)
        with self.session_factory() as session:
            row = self._find(session, payload.request_id)
            if row is None:
                return None
            return self._stored_response(
                row,
                request_hash=request_hash,
            )

    def accept(
        self,
        payload: ChatFeedbackRequest,
        *,
        verified_target: dict[str, Any],
    ) -> FeedbackHttpResponse:
        self._validate_target(payload, verified_target)
        request_hash = self.cipher.request_digest(payload)
        replay = self.replay(payload)
        if replay is not None:
            return replay

        patient_id_hash = self.cipher.patient_digest(payload.patient_id)
        context = feedback_encryption_context(
            payload,
            patient_id_hash=patient_id_hash,
        )
        feedback_text = payload.feedback_text
        ciphertext = (
            self.cipher.encrypt(feedback_text, context=context)
            if feedback_text is not None
            else ""
        )
        text_hash = (
            self.cipher.text_digest(feedback_text)
            if feedback_text is not None
            else ""
        )

        for _ in range(3):
            with self.session_factory() as session:
                # Refresh per insert attempt. If a concurrent reaction wins
                # the partial-unique race, the retry that supersedes it must
                # carry a later ordering timestamp.
                now = utc_now()
                existing = self._find(
                    session,
                    payload.request_id,
                    lock=True,
                )
                if existing is not None:
                    return self._stored_response(
                        existing,
                        request_hash=request_hash,
                    )
                if payload.reaction is not None:
                    self._clear_current_reaction(
                        session,
                        payload=payload,
                        patient_id_hash=patient_id_hash,
                        updated_at=now,
                    )
                response = ChatFeedbackAccepted(
                    status="accepted",
                    # The response describes this request, not the currently
                    # active reaction from an earlier request. The v1.3
                    # contract requires null for an opinion-only submission.
                    reaction=payload.reaction,
                    accepted_at=now.replace(tzinfo=UTC),
                ).model_dump(mode="json")
                response_json = json.dumps(
                    response,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                row = AgentFeedbackLink(
                    api_path=FEEDBACK_API_PATH,
                    request_id=payload.request_id,
                    request_hash=request_hash,
                    message_id=payload.message_id,
                    patient_id_hash=patient_id_hash,
                    trace_id=str(verified_target.get("trace_id") or "")[:160],
                    feedback=_reaction_storage_value(payload.reaction),
                    feedback_text_ciphertext=ciphertext,
                    feedback_text_hash=text_hash,
                    encryption_key_id=(
                        self.cipher.key_id
                        if feedback_text is not None
                        else ""
                    ),
                    status=ACCEPTED,
                    error_code="",
                    response_status=202,
                    response_json=response_json,
                    attempt_count=0,
                    max_attempts=self.max_attempts,
                    feedback_at=_database_time(payload.feedback_at),
                    next_attempt_at=now,
                    last_attempt_at=None,
                    processed_at=None,
                    created_at=now,
                    updated_at=now,
                    expires_at=now
                    + timedelta(seconds=self.retention_seconds),
                )
                session.add(row)
                enqueue_user_feedback_score(
                    session,
                    request_id=payload.request_id,
                    message_id=payload.message_id,
                    patient_id_hash=patient_id_hash,
                    trace_id=row.trace_id,
                    feedback=row.feedback,
                    feedback_at=payload.feedback_at,
                    feedback_text_present=feedback_text is not None,
                    settings=self.settings,
                    recorded_at=now,
                )
                try:
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    continue
                return FeedbackHttpResponse(
                    status_code=202,
                    body=response,
                )
        replay = self.replay(payload)
        if replay is not None:
            return replay
        raise RuntimeError("feedback_concurrent_insert_retry_exhausted")

    def claim_due(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[FeedbackWorkItem]:
        current = now or utc_now()
        with self.session_factory() as session:
            session.execute(
                update(AgentFeedbackLink)
                .where(
                    AgentFeedbackLink.status == PROCESSING,
                    AgentFeedbackLink.next_attempt_at <= current,
                    AgentFeedbackLink.attempt_count
                    >= AgentFeedbackLink.max_attempts,
                )
                .values(
                    status=FINAL_FAILED,
                    error_code="PROCESSING_LEASE_EXPIRED",
                    next_attempt_at=None,
                    processed_at=current,
                    updated_at=current,
                )
            )
            rows = list(
                session.scalars(
                    select(AgentFeedbackLink)
                    .where(
                        or_(
                            and_(
                                AgentFeedbackLink.status.in_(
                                    (ACCEPTED, RETRYABLE_FAILED)
                                ),
                                AgentFeedbackLink.next_attempt_at <= current,
                            ),
                            and_(
                                AgentFeedbackLink.status == PROCESSING,
                                AgentFeedbackLink.next_attempt_at <= current,
                                AgentFeedbackLink.attempt_count
                                < AgentFeedbackLink.max_attempts,
                            ),
                        ),
                        AgentFeedbackLink.attempt_count
                        < AgentFeedbackLink.max_attempts,
                    )
                    .order_by(
                        AgentFeedbackLink.next_attempt_at,
                        AgentFeedbackLink.id,
                    )
                    .limit(max(1, min(int(limit), 500)))
                    .with_for_update(skip_locked=True)
                ).all()
            )
            result: list[FeedbackWorkItem] = []
            for row in rows:
                row.status = PROCESSING
                row.attempt_count += 1
                row.last_attempt_at = current
                row.next_attempt_at = current + timedelta(
                    seconds=self.processing_lease_seconds
                )
                row.error_code = ""
                row.updated_at = current
                result.append(
                    FeedbackWorkItem(
                        record_id=row.id,
                        request_id=row.request_id,
                        attempt_count=row.attempt_count,
                        max_attempts=row.max_attempts,
                    )
                )
            session.commit()
            return result

    def complete(
        self,
        record_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.session_factory() as session:
            row = self._required_processing_row(session, record_id)
            row.status = COMPLETED
            row.error_code = ""
            row.processed_at = current
            row.next_attempt_at = None
            row.updated_at = current
            session.commit()

    def fail(
        self,
        record_id: int,
        *,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        normalized_error_code = error_code.strip()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", normalized_error_code) is None:
            raise ValueError("feedback_error_code_invalid")
        with self.session_factory() as session:
            row = self._required_processing_row(session, record_id)
            should_retry = (
                retryable and row.attempt_count < row.max_attempts
            )
            row.status = (
                RETRYABLE_FAILED if should_retry else FINAL_FAILED
            )
            row.error_code = normalized_error_code
            row.next_attempt_at = (
                current
                + timedelta(
                    seconds=min(
                        self.retry_max_seconds,
                        self.retry_base_seconds
                        * (2 ** max(0, row.attempt_count - 1)),
                    )
                )
                if should_retry
                else None
            )
            row.processed_at = None if should_retry else current
            row.updated_at = current
            session.commit()

    @staticmethod
    def _find(
        session: Session,
        request_id: str,
        *,
        lock: bool = False,
    ) -> AgentFeedbackLink | None:
        statement = select(AgentFeedbackLink).where(
            AgentFeedbackLink.api_path == FEEDBACK_API_PATH,
            AgentFeedbackLink.request_id == request_id,
        )
        if lock:
            statement = statement.with_for_update()
        return session.scalar(statement)

    @staticmethod
    def _stored_response(
        row: AgentFeedbackLink,
        *,
        request_hash: str,
    ) -> FeedbackHttpResponse:
        if not row.request_hash or not hmac_compare(
            row.request_hash,
            request_hash,
        ):
            raise FeedbackIdempotencyConflict(
                "feedback_request_id_reused_with_different_body"
            )
        try:
            body = json.loads(row.response_json)
            ChatFeedbackAccepted.model_validate(body)
        except Exception as exc:
            raise RuntimeError(
                "feedback_stored_response_invalid"
            ) from exc
        return FeedbackHttpResponse(
            status_code=int(row.response_status),
            body=body,
        )

    @staticmethod
    def _clear_current_reaction(
        session: Session,
        *,
        payload: ChatFeedbackRequest,
        patient_id_hash: str,
        updated_at: datetime,
    ) -> None:
        session.execute(
            update(AgentFeedbackLink)
            .where(
                AgentFeedbackLink.api_path == FEEDBACK_API_PATH,
                AgentFeedbackLink.message_id == payload.message_id,
                AgentFeedbackLink.patient_id_hash == patient_id_hash,
                AgentFeedbackLink.feedback.is_not(None),
            )
            .values(
                feedback=None,
                updated_at=updated_at,
            )
        )

    @staticmethod
    def _current_reaction(
        session: Session,
        *,
        payload: ChatFeedbackRequest,
        patient_id_hash: str,
    ) -> str | None:
        value = session.scalar(
            select(AgentFeedbackLink.feedback)
            .where(
                AgentFeedbackLink.api_path == FEEDBACK_API_PATH,
                AgentFeedbackLink.message_id == payload.message_id,
                AgentFeedbackLink.patient_id_hash == patient_id_hash,
                AgentFeedbackLink.feedback.is_not(None),
            )
            .order_by(AgentFeedbackLink.id.desc())
            .limit(1)
        )
        return _reaction_contract_value(value)

    @staticmethod
    def _validate_target(
        payload: ChatFeedbackRequest,
        target: dict[str, Any],
    ) -> None:
        actual = (
            str(target.get("message_id") or ""),
            str(target.get("patient_id") or ""),
            str(target.get("role") or "").lower(),
        )
        expected = (
            payload.message_id,
            payload.patient_id,
            "assistant",
        )
        if actual != expected:
            raise FeedbackTargetMismatch(
                "backend_feedback_target_context_mismatch"
            )

    @staticmethod
    def _required_processing_row(
        session: Session,
        record_id: int,
    ) -> AgentFeedbackLink:
        row = session.scalar(
            select(AgentFeedbackLink)
            .where(AgentFeedbackLink.id == record_id)
            .with_for_update()
        )
        if row is None:
            raise RuntimeError("feedback_record_not_found")
        if row.status != PROCESSING:
            raise RuntimeError("feedback_record_not_processing")
        return row


def feedback_encryption_context(
    payload: ChatFeedbackRequest,
    *,
    patient_id_hash: str,
) -> FeedbackEncryptionContext:
    return FeedbackEncryptionContext(
        api_path=FEEDBACK_API_PATH,
        request_id=payload.request_id,
        message_id=payload.message_id,
        patient_id_hash=patient_id_hash,
        feedback_at=_feedback_time_text(payload.feedback_at),
    )


def _feedback_time_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _database_time(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _reaction_storage_value(
    reaction: str | None,
) -> bool | None:
    if reaction == "like":
        return True
    if reaction == "dislike":
        return False
    return None


def _reaction_contract_value(
    value: bool | None,
) -> str | None:
    if value is None:
        return None
    return "like" if bool(value) else "dislike"


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
