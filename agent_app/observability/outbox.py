from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.persistence.models import (
    AgentObservabilityExport,
    AgentRunStep,
    AgentRunTrace,
)
from shared.json_utils import dump_json, parse_json_object
from shared.redaction import redact_inline_secrets
from shared.settings import Settings
from shared.time_utils import utc_now

DESTINATION_LANGFUSE = "langfuse"
TRACE_ATTEMPT = "TRACE_ATTEMPT"
TRACE_DELETE = "TRACE_DELETE"
SCORE = "SCORE"

PENDING = "PENDING"
PROCESSING = "PROCESSING"
RETRYABLE_FAILED = "RETRYABLE_FAILED"
COMPLETED = "COMPLETED"
DEAD = "DEAD"
SKIPPED = "SKIPPED"

_SUCCESS_STATUSES = {"COMPLETED", "SUCCESS", "SUCCEEDED"}
_READ_ONLY_SIDE_EFFECTS = {"", "none", "read_only", "readonly"}


@dataclass(frozen=True)
class ObservabilityExportWorkItem:
    id: int
    event_key: str
    event_type: str
    trace_id: str
    trace_attempt_number: int
    payload: dict[str, Any]
    attempt_count: int
    max_attempts: int


def deterministic_langfuse_trace_id(trace_id: str) -> str:
    """Return a stable W3C-compatible 32-character lowercase trace id."""

    return hashlib.sha256(
        f"dranswer-langfuse-trace:{trace_id}".encode("utf-8")
    ).hexdigest()[:32]


def deterministic_span_id(observation_id: str) -> str:
    return hashlib.sha256(
        f"dranswer-langfuse-span:{observation_id}".encode("utf-8")
    ).hexdigest()[:16]


def enqueue_trace_attempt(
    session: Session,
    *,
    trace: AgentRunTrace,
    settings: Settings,
    recorded_at: datetime | None = None,
) -> None:
    """Atomically stage one immutable attempt and its automatic scores."""

    if not getattr(settings, "langfuse_export_enabled", False):
        return
    current = recorded_at or utc_now()
    should_export = should_export_trace_attempt(
        session,
        trace=trace,
        settings=settings,
    )
    _enqueue(
        session,
        event_key=f"trace:{trace.trace_id}:{trace.attempt_count}",
        event_type=TRACE_ATTEMPT,
        trace_id=trace.trace_id,
        trace_attempt_number=trace.attempt_count,
        payload={},
        status=PENDING if should_export else SKIPPED,
        settings=settings,
        current=current,
    )
    if not should_export:
        return

    metadata = parse_json_object(trace.metadata_json)
    completed = trace.status == "COMPLETED"
    validation_passed = bool(metadata.get("validation_passed"))
    _enqueue_score(
        session,
        trace=trace,
        name="agent_success",
        value=bool(completed and validation_passed),
        data_type="BOOLEAN",
        version=f"attempt:{trace.attempt_count}",
        timestamp=trace.completed_at or current,
        settings=settings,
        metadata={
            "source": "agent_runtime",
            "attempt": trace.attempt_count,
        },
        current=current,
    )
    if completed:
        _enqueue_score(
            session,
            trace=trace,
            name="response_validation",
            value=validation_passed,
            data_type="BOOLEAN",
            version=f"attempt:{trace.attempt_count}",
            timestamp=trace.completed_at or current,
            settings=settings,
            metadata={
                "source": "response_contract",
                "attempt": trace.attempt_count,
            },
            current=current,
        )

    tool_steps = list(
        session.scalars(
            select(AgentRunStep).where(
                AgentRunStep.trace_id == trace.trace_id,
                AgentRunStep.trace_attempt_number == trace.attempt_count,
                AgentRunStep.step_type == "tool_call",
            )
        ).all()
    )
    if tool_steps:
        success_count = sum(
            1 for step in tool_steps if step.status.upper() in _SUCCESS_STATUSES
        )
        _enqueue_score(
            session,
            trace=trace,
            name="tool_success_rate",
            value=float(success_count / len(tool_steps)),
            data_type="NUMERIC",
            version=f"attempt:{trace.attempt_count}",
            timestamp=trace.completed_at or current,
            settings=settings,
            metadata={
                "source": "tool_runtime",
                "attempt": trace.attempt_count,
                "tool_count": len(tool_steps),
            },
            current=current,
        )

    _enqueue_score(
        session,
        trace=trace,
        name="retry_count",
        value=float(max(0, trace.attempt_count - 1)),
        data_type="NUMERIC",
        version=f"attempt:{trace.attempt_count}",
        timestamp=trace.completed_at or current,
        settings=settings,
        metadata={"source": "agent_runtime"},
        current=current,
        stable_score_id=True,
    )


def enqueue_user_feedback_score(
    session: Session,
    *,
    request_id: str,
    message_id: str,
    patient_id_hash: str,
    trace_id: str,
    feedback: bool | None,
    feedback_at: datetime,
    feedback_text_present: bool,
    settings: Settings,
    recorded_at: datetime | None = None,
) -> None:
    """Export only the structured reaction; encrypted free text stays local."""

    if (
        not getattr(settings, "langfuse_export_enabled", False)
        or feedback is None
        or not trace_id
    ):
        return
    current = recorded_at or utc_now()
    _promote_skipped_trace(session, trace_id=trace_id, current=current)
    _ensure_trace_export_for_feedback(
        session,
        trace_id=trace_id,
        settings=settings,
        current=current,
    )
    score_id = _stable_uuid(
        f"user_feedback:{patient_id_hash}:{message_id}"
    )
    payload = {
        "id": score_id,
        "traceId": deterministic_langfuse_trace_id(trace_id),
        "name": "user_feedback",
        "value": bool(feedback),
        "dataType": "BOOLEAN",
        "timestamp": _isoformat(feedback_at),
        "metadata": {
            "source": "end_user",
            "feedbackTextPresent": bool(feedback_text_present),
        },
    }
    _enqueue(
        session,
        event_key=f"score:user_feedback:{request_id}",
        event_type=SCORE,
        trace_id=trace_id,
        trace_attempt_number=0,
        payload=payload,
        status=PENDING,
        settings=settings,
        current=current,
    )


def enqueue_trace_delete(
    session: Session,
    *,
    trace_id: str,
    settings: Settings,
    recorded_at: datetime | None = None,
) -> None:
    if not getattr(settings, "langfuse_export_enabled", False):
        return
    current = recorded_at or utc_now()
    _enqueue(
        session,
        event_key=f"delete:{trace_id}",
        event_type=TRACE_DELETE,
        trace_id=trace_id,
        trace_attempt_number=0,
        payload={
            "traceId": deterministic_langfuse_trace_id(trace_id),
            "reason": "retention_expired",
        },
        status=PENDING,
        settings=settings,
        current=current,
    )


def should_export_trace_attempt(
    session: Session,
    *,
    trace: AgentRunTrace,
    settings: Settings,
) -> bool:
    """Deterministic sampling with safety- and write-path overrides."""

    if trace.status != "COMPLETED":
        return True
    if trace.workflow_name == "missed_dose":
        return True
    has_write = session.scalar(
        select(AgentRunStep.id)
        .where(
            AgentRunStep.trace_id == trace.trace_id,
            AgentRunStep.trace_attempt_number == trace.attempt_count,
            AgentRunStep.step_type == "tool_call",
            func.lower(AgentRunStep.side_effect_level).notin_(
                tuple(_READ_ONLY_SIDE_EFFECTS)
            ),
        )
        .limit(1)
    )
    if has_write is not None:
        return True
    rate = float(settings.langfuse_success_sample_rate)
    if rate >= 1:
        return True
    if rate <= 0:
        return False
    bucket = int(
        hashlib.sha256(
            f"{trace.trace_id}:{trace.attempt_count}".encode("utf-8")
        ).hexdigest()[:8],
        16,
    ) / 0xFFFFFFFF
    return bucket < rate


class ObservabilityOutbox:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings

    def claim_due(
        self,
        *,
        worker_id: str,
        limit: int | None = None,
        now: datetime | None = None,
    ) -> list[ObservabilityExportWorkItem]:
        current = now or utc_now()
        safe_limit = max(
            1,
            min(
                int(limit or self.settings.langfuse_export_batch_size),
                200,
            ),
        )
        with self.session_factory() as session:
            session.execute(
                update(AgentObservabilityExport)
                .where(
                    AgentObservabilityExport.destination
                    == DESTINATION_LANGFUSE,
                    AgentObservabilityExport.status == PROCESSING,
                    AgentObservabilityExport.lease_expires_at <= current,
                    AgentObservabilityExport.attempt_count
                    >= AgentObservabilityExport.max_attempts,
                )
                .values(
                    status=DEAD,
                    next_attempt_at=None,
                    lease_expires_at=None,
                    locked_by="",
                    last_error_code="PROCESSING_LEASE_EXPIRED",
                    last_error_message="export processing lease expired",
                    updated_at=current,
                )
            )
            rows = list(
                session.scalars(
                    select(AgentObservabilityExport)
                    .where(
                        AgentObservabilityExport.destination
                        == DESTINATION_LANGFUSE,
                        or_(
                            and_(
                                AgentObservabilityExport.status.in_(
                                    (PENDING, RETRYABLE_FAILED)
                                ),
                                AgentObservabilityExport.next_attempt_at
                                <= current,
                            ),
                            and_(
                                AgentObservabilityExport.status == PROCESSING,
                                AgentObservabilityExport.lease_expires_at
                                <= current,
                            ),
                        ),
                        AgentObservabilityExport.attempt_count
                        < AgentObservabilityExport.max_attempts,
                    )
                    .order_by(
                        AgentObservabilityExport.next_attempt_at,
                        AgentObservabilityExport.id,
                    )
                    .limit(safe_limit)
                    .with_for_update(skip_locked=True)
                ).all()
            )
            items: list[ObservabilityExportWorkItem] = []
            for row in rows:
                row.status = PROCESSING
                row.attempt_count += 1
                row.next_attempt_at = None
                row.locked_by = worker_id[:160]
                row.lease_expires_at = current + timedelta(
                    seconds=self.settings.langfuse_export_lease_seconds
                )
                row.last_error_code = ""
                row.last_error_message = ""
                row.updated_at = current
                items.append(
                    ObservabilityExportWorkItem(
                        id=row.id,
                        event_key=row.event_key,
                        event_type=row.event_type,
                        trace_id=row.trace_id,
                        trace_attempt_number=row.trace_attempt_number,
                        payload=parse_json_object(row.payload_json),
                        attempt_count=row.attempt_count,
                        max_attempts=row.max_attempts,
                    )
                )
            session.commit()
            return items

    def complete(
        self,
        item_id: int,
        *,
        now: datetime | None = None,
    ) -> None:
        current = now or utc_now()
        with self.session_factory() as session:
            row = _required_processing_row(session, item_id)
            row.status = COMPLETED
            row.next_attempt_at = None
            row.lease_expires_at = None
            row.locked_by = ""
            row.last_error_code = ""
            row.last_error_message = ""
            row.exported_at = current
            row.updated_at = current
            session.commit()

    def fail(
        self,
        item_id: int,
        *,
        error_code: str,
        error_message: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> bool:
        current = now or utc_now()
        normalized_code = error_code.strip().upper()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", normalized_code) is None:
            normalized_code = "EXPORT_FAILED"
        with self.session_factory() as session:
            row = _required_processing_row(session, item_id)
            should_retry = retryable and row.attempt_count < row.max_attempts
            row.status = RETRYABLE_FAILED if should_retry else DEAD
            row.next_attempt_at = (
                current
                + timedelta(
                    seconds=_retry_delay_seconds(
                        row.event_key,
                        row.attempt_count,
                        base=self.settings.langfuse_export_retry_base_seconds,
                        maximum=self.settings.langfuse_export_retry_max_seconds,
                    )
                )
                if should_retry
                else None
            )
            row.lease_expires_at = None
            row.locked_by = ""
            row.last_error_code = normalized_code
            row.last_error_message = redact_inline_secrets(
                error_message
            )[:500]
            row.updated_at = current
            session.commit()
            return should_retry


def _enqueue_score(
    session: Session,
    *,
    trace: AgentRunTrace,
    name: str,
    value: bool | float,
    data_type: str,
    version: str,
    timestamp: datetime,
    settings: Settings,
    metadata: dict[str, Any],
    current: datetime,
    stable_score_id: bool = False,
) -> None:
    score_seed = f"{trace.trace_id}:{name}"
    score_id = _stable_uuid(
        score_seed if stable_score_id else f"{score_seed}:{version}"
    )
    payload = {
        "id": score_id,
        "traceId": deterministic_langfuse_trace_id(trace.trace_id),
        "name": name,
        "value": value,
        "dataType": data_type,
        "timestamp": _isoformat(timestamp),
        "environment": trace.environment,
        "metadata": metadata,
    }
    _enqueue(
        session,
        event_key=f"score:{name}:{trace.trace_id}:{version}",
        event_type=SCORE,
        trace_id=trace.trace_id,
        trace_attempt_number=trace.attempt_count,
        payload=payload,
        status=PENDING,
        settings=settings,
        current=current,
    )


def _enqueue(
    session: Session,
    *,
    event_key: str,
    event_type: str,
    trace_id: str,
    trace_attempt_number: int,
    payload: dict[str, Any],
    status: str,
    settings: Settings,
    current: datetime,
) -> AgentObservabilityExport:
    existing = session.scalar(
        select(AgentObservabilityExport).where(
            AgentObservabilityExport.destination == DESTINATION_LANGFUSE,
            AgentObservabilityExport.event_key == event_key,
        )
    )
    if existing is not None:
        return existing
    row = AgentObservabilityExport(
        destination=DESTINATION_LANGFUSE,
        event_key=event_key[:255],
        event_type=event_type,
        trace_id=trace_id[:160],
        trace_attempt_number=max(0, int(trace_attempt_number)),
        payload_json=dump_json(payload),
        status=status,
        attempt_count=0,
        max_attempts=settings.langfuse_export_max_attempts,
        next_attempt_at=current if status == PENDING else None,
        locked_by="",
        lease_expires_at=None,
        last_error_code="",
        last_error_message="",
        remote_trace_id=deterministic_langfuse_trace_id(trace_id),
        created_at=current,
        updated_at=current,
        exported_at=None,
        expires_at=current
        + timedelta(seconds=settings.langfuse_outbox_retention_seconds),
    )
    session.add(row)
    return row


def _promote_skipped_trace(
    session: Session,
    *,
    trace_id: str,
    current: datetime,
) -> None:
    session.execute(
        update(AgentObservabilityExport)
        .where(
            AgentObservabilityExport.destination == DESTINATION_LANGFUSE,
            AgentObservabilityExport.trace_id == trace_id,
            AgentObservabilityExport.event_type == TRACE_ATTEMPT,
            AgentObservabilityExport.status == SKIPPED,
        )
        .values(
            status=PENDING,
            next_attempt_at=current,
            updated_at=current,
        )
    )


def _ensure_trace_export_for_feedback(
    session: Session,
    *,
    trace_id: str,
    settings: Settings,
    current: datetime,
) -> None:
    if session.scalar(
        select(AgentObservabilityExport.id)
        .where(
            AgentObservabilityExport.destination == DESTINATION_LANGFUSE,
            AgentObservabilityExport.trace_id == trace_id,
            AgentObservabilityExport.event_type == TRACE_ATTEMPT,
            AgentObservabilityExport.status.notin_((DEAD,)),
        )
        .limit(1)
    ) is not None:
        return
    dead_event = session.scalar(
        select(AgentObservabilityExport)
        .where(
            AgentObservabilityExport.destination == DESTINATION_LANGFUSE,
            AgentObservabilityExport.trace_id == trace_id,
            AgentObservabilityExport.event_type == TRACE_ATTEMPT,
            AgentObservabilityExport.status == DEAD,
        )
        .order_by(AgentObservabilityExport.trace_attempt_number.desc())
        .limit(1)
    )
    if dead_event is not None:
        dead_event.status = PENDING
        dead_event.attempt_count = 0
        dead_event.next_attempt_at = current
        dead_event.lease_expires_at = None
        dead_event.locked_by = ""
        dead_event.last_error_code = ""
        dead_event.last_error_message = ""
        dead_event.updated_at = current
        return
    trace = session.scalar(
        select(AgentRunTrace).where(AgentRunTrace.trace_id == trace_id)
    )
    if trace is None:
        return
    _enqueue(
        session,
        event_key=f"trace:{trace.trace_id}:{trace.attempt_count}",
        event_type=TRACE_ATTEMPT,
        trace_id=trace.trace_id,
        trace_attempt_number=trace.attempt_count,
        payload={},
        status=PENDING,
        settings=settings,
        current=current,
    )


def _required_processing_row(
    session: Session,
    item_id: int,
) -> AgentObservabilityExport:
    row = session.scalar(
        select(AgentObservabilityExport)
        .where(AgentObservabilityExport.id == item_id)
        .with_for_update()
    )
    if row is None:
        raise RuntimeError("observability_export_not_found")
    if row.status != PROCESSING:
        raise RuntimeError("observability_export_not_processing")
    return row


def _retry_delay_seconds(
    event_key: str,
    attempt_count: int,
    *,
    base: int,
    maximum: int,
) -> float:
    exponential = min(maximum, base * (2 ** max(0, attempt_count - 1)))
    jitter_window = min(5.0, exponential * 0.2)
    jitter_bucket = int(
        hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:8],
        16,
    ) / 0xFFFFFFFF
    return float(exponential + (jitter_bucket * jitter_window))


def _stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dranswer:{seed}"))


def _isoformat(value: datetime) -> str:
    return value.isoformat()
