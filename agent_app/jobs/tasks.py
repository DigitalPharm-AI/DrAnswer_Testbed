from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_app.persistence.models import AgentAsyncTask
from shared.json_utils import dump_json, parse_json_object
from shared.redaction import redact_for_logging, redact_inline_secrets
from shared.settings import get_settings
from shared.time_utils import utc_now

PENDING = "pending"
RUNNING = "running"
CALLBACK_SENT = "callback_sent"
DONE = "done"
FAILED = "failed"
DEAD = "dead"

ACTIVE_STATUSES = {PENDING, RUNNING, CALLBACK_SENT}


def enqueue_async_task(
    session: Session,
    *,
    request_id: str,
    task_type: str,
    payload: dict[str, Any],
    callback_context: dict[str, Any] | None = None,
    max_attempts: int | None = None,
    run_after: datetime | None = None,
    deduplication_key: str | None = None,
) -> tuple[AgentAsyncTask, bool]:
    if deduplication_key is not None and not deduplication_key.strip():
        raise ValueError("async_task_deduplication_key_empty")
    existing_by_request = session.scalar(
        select(AgentAsyncTask).where(
            AgentAsyncTask.request_id == request_id,
        ),
    )
    if existing_by_request is not None:
        if existing_by_request.deduplication_key != deduplication_key:
            raise ValueError("async_task_identity_collision")
        return existing_by_request, False
    if deduplication_key is not None:
        existing_by_deduplication = session.scalar(
            select(AgentAsyncTask).where(
                AgentAsyncTask.deduplication_key == deduplication_key,
            ),
        )
        if existing_by_deduplication is not None:
            # The first request_id remains canonical. A later scheduler
            # invocation is a duplicate by its durable business key.
            return existing_by_deduplication, False
    accepted_at = utc_now()
    task = AgentAsyncTask(
        request_id=request_id,
        deduplication_key=deduplication_key,
        task_type=task_type,
        status=PENDING,
        payload_json=json.dumps(payload, ensure_ascii=False),
        callback_context_json=dump_json(callback_context or {}),
        accepted_at=accepted_at,
        run_after=run_after or accepted_at,
        max_attempts=max_attempts or get_settings().agent_task_max_attempts,
        expires_at=accepted_at
        + timedelta(
            seconds=get_settings().agent_async_task_retention_seconds
        ),
    )
    try:
        # The unique request_id constraint is the cross-process arbiter. The
        # savepoint lets the losing concurrent submit recover as a duplicate
        # without poisoning the caller's outer transaction.
        with session.begin_nested():
            session.add(task)
            session.flush()
    except IntegrityError:
        existing_by_request = session.scalar(
            select(AgentAsyncTask).where(
                AgentAsyncTask.request_id == request_id,
            ),
        )
        if existing_by_request is not None:
            if existing_by_request.deduplication_key != deduplication_key:
                raise ValueError("async_task_identity_collision")
            return existing_by_request, False
        existing_by_deduplication = (
            session.scalar(
                select(AgentAsyncTask).where(
                    AgentAsyncTask.deduplication_key
                    == deduplication_key,
                ),
            )
            if deduplication_key is not None
            else None
        )
        if existing_by_deduplication is None:
            raise
        return existing_by_deduplication, False
    return task, True


def claim_next_async_task(
    session: Session,
    *,
    worker_id: str = "",
    visibility_timeout_seconds: int | None = None,
) -> AgentAsyncTask | None:
    now = utc_now()
    _mark_exhausted_stale_running_tasks(session, now)
    query = (
        select(AgentAsyncTask)
        .where(
            or_(
                and_(
                    AgentAsyncTask.status == PENDING,
                    or_(AgentAsyncTask.run_after.is_(None), AgentAsyncTask.run_after <= now),
                ),
                and_(
                    AgentAsyncTask.status == RUNNING,
                    AgentAsyncTask.locked_until.is_not(None),
                    AgentAsyncTask.locked_until <= now,
                ),
                and_(
                    AgentAsyncTask.status == CALLBACK_SENT,
                    AgentAsyncTask.locked_until.is_not(None),
                    AgentAsyncTask.locked_until <= now,
                ),
            ),
            AgentAsyncTask.attempts < AgentAsyncTask.max_attempts,
        )
        .order_by(AgentAsyncTask.accepted_at.asc(), AgentAsyncTask.id.asc())
        .limit(1)
    )
    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)
    task = session.scalar(query)
    if task is None:
        return None
    timeout = visibility_timeout_seconds or get_settings().agent_task_visibility_timeout_seconds
    task.status = RUNNING
    task.attempts += 1
    task.started_at = now
    task.locked_by = worker_id
    task.locked_until = now + timedelta(seconds=timeout)
    task.last_error = ""
    session.flush()
    return task


def mark_callback_sent(session: Session, task_id: int) -> None:
    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return
    task.status = CALLBACK_SENT
    task.locked_until = utc_now() + timedelta(seconds=60)
    session.flush()


def mark_async_task_done(session: Session, task_id: int) -> None:
    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return
    task.status = DONE
    task.completed_at = utc_now()
    task.last_error = ""
    task.locked_by = ""
    task.locked_until = None
    task.run_after = None
    session.flush()


def mark_async_task_failed(session: Session, task_id: int, message: str) -> tuple[AgentAsyncTask | None, bool]:
    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return None, False
    now = utc_now()
    task.last_error = message
    task.locked_by = ""
    task.locked_until = None
    if task.attempts >= task.max_attempts:
        task.status = DEAD
        task.completed_at = now
        task.run_after = None
        session.flush()
        return task, True
    task.status = PENDING
    task.completed_at = None
    task.run_after = now + timedelta(seconds=_retry_delay_seconds(task.attempts))
    session.flush()
    return task, False


def mark_async_task_terminal_failure(
    session: Session,
    task_id: int,
    message: str,
) -> AgentAsyncTask | None:
    """Fail without automatic retry when an in-flight provider call is uncertain."""

    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return None
    task.status = DEAD
    task.last_error = message
    task.completed_at = utc_now()
    task.locked_by = ""
    task.locked_until = None
    task.run_after = None
    session.flush()
    return task


def stage_async_task_callback_delivery(
    session: Session,
    task_id: int,
    *,
    callback_payload: dict[str, Any],
    callback_path: str,
    callback_bearer: bool,
    processing_error: str,
) -> AgentAsyncTask | None:
    """Persist a terminal failure callback and give delivery its own retries."""

    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return None
    payload = task_payload(task)
    if not isinstance(payload.get("_callback_payload"), dict):
        payload["_callback_payload"] = callback_payload
        payload["_callback_path"] = callback_path
        payload["_callback_bearer"] = callback_bearer
        payload["_processing_failure"] = redact_inline_secrets(
            processing_error,
        )
        task.payload_json = json.dumps(payload, ensure_ascii=False)
    now = utc_now()
    task.status = PENDING
    task.attempts = 0
    task.completed_at = None
    task.started_at = None
    task.locked_by = ""
    task.locked_until = None
    task.run_after = now
    task.last_error = "terminal_result_callback_delivery_pending"
    session.flush()
    return task


def reset_running_async_tasks(session: Session) -> int:
    tasks = session.scalars(select(AgentAsyncTask).where(AgentAsyncTask.status.in_([RUNNING, CALLBACK_SENT]))).all()
    now = utc_now()
    for task in tasks:
        task.status = PENDING
        task.last_error = "서버 재시작 후 대기 상태로 복구되었습니다."
        task.started_at = None
        task.locked_by = ""
        task.locked_until = None
        task.run_after = now
    session.flush()
    return len(tasks)


def retry_dead_async_task(session: Session, task: AgentAsyncTask, *, reason: str = "") -> AgentAsyncTask:
    now = utc_now()
    task.status = PENDING
    task.attempts = 0
    task.started_at = None
    task.completed_at = None
    task.locked_by = ""
    task.locked_until = None
    task.run_after = now
    task.last_error = _operator_action_message("operator_retry_requested", reason)
    session.flush()
    return task


def dismiss_dead_async_task(session: Session, task: AgentAsyncTask, *, reason: str = "") -> AgentAsyncTask:
    task.status = FAILED
    task.completed_at = utc_now()
    task.locked_by = ""
    task.locked_until = None
    task.run_after = None
    task.last_error = _operator_action_message("operator_dismissed", reason)
    session.flush()
    return task


def task_payload(task: AgentAsyncTask) -> dict[str, Any]:
    payload = json.loads(task.payload_json)
    return payload if isinstance(payload, dict) else {}


def task_callback_context(task: AgentAsyncTask) -> dict[str, Any]:
    return parse_json_object(task.callback_context_json)


def async_task_rows(session: Session, *, status: str | None = None, limit: int = 50) -> list[AgentAsyncTask]:
    safe_limit = max(1, min(limit, 200))
    query = select(AgentAsyncTask)
    if status:
        query = query.where(AgentAsyncTask.status == status)
    query = query.order_by(AgentAsyncTask.accepted_at.desc(), AgentAsyncTask.id.desc()).limit(safe_limit)
    return list(session.scalars(query).all())


def async_task_by_request_id(session: Session, request_id: str) -> AgentAsyncTask | None:
    return session.scalar(select(AgentAsyncTask).where(AgentAsyncTask.request_id == request_id))


def async_task_observability_payload(task: AgentAsyncTask) -> dict[str, Any]:
    now = utc_now()
    payload_keys, payload_parse_error = _safe_payload_keys(task)
    runtime_reference = task.completed_at if task.completed_at is not None else now
    return {
        "id": task.id,
        "request_id": task.request_id,
        "task_type": task.task_type,
        "status": task.status,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "last_error": redact_inline_secrets(task.last_error or ""),
        "age_seconds": _seconds_since(task.accepted_at, now),
        "runtime_seconds": _seconds_elapsed(task.started_at, runtime_reference),
        "next_retry_in_seconds": _seconds_until(task.run_after, now),
        "is_locked": task.locked_until is not None and task.locked_until > now,
        "is_retry_due": task.run_after is None or task.run_after <= now,
        "accepted_at": _isoformat(task.accepted_at),
        "run_after": _isoformat(task.run_after),
        "started_at": _isoformat(task.started_at),
        "completed_at": _isoformat(task.completed_at),
        "locked_by": task.locked_by,
        "locked_until": _isoformat(task.locked_until),
        "payload_keys": payload_keys,
        "payload_parse_error": payload_parse_error,
        "callback_context": redact_for_logging(task_callback_context(task)),
    }


def async_task_status_counts(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(AgentAsyncTask.status, func.count(AgentAsyncTask.id)).group_by(AgentAsyncTask.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def _retry_delay_seconds(attempts: int) -> int:
    settings = get_settings()
    exponent = max(0, attempts - 1)
    return min(
        settings.agent_task_retry_max_seconds,
        settings.agent_task_retry_base_seconds * (2**exponent),
    )


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _seconds_since(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def _seconds_elapsed(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _seconds_until(value: datetime | None, now: datetime) -> int | None:
    if value is None:
        return None
    return max(0, int((value - now).total_seconds()))


def _safe_payload_keys(task: AgentAsyncTask) -> tuple[list[str], bool]:
    try:
        payload = task_payload(task)
    except Exception:
        return [], True
    return sorted(str(key) for key in payload.keys()), False


def _operator_action_message(action: str, reason: str) -> str:
    safe_reason = redact_inline_secrets(reason.strip())
    if not safe_reason:
        return action
    return f"{action}: {safe_reason}"


def _mark_exhausted_stale_running_tasks(session: Session, now: datetime) -> None:
    tasks = session.scalars(
        select(AgentAsyncTask).where(
            AgentAsyncTask.status.in_([RUNNING, CALLBACK_SENT]),
            AgentAsyncTask.locked_until.is_not(None),
            AgentAsyncTask.locked_until <= now,
            AgentAsyncTask.attempts >= AgentAsyncTask.max_attempts,
        )
    ).all()
    for task in tasks:
        task.status = DEAD
        task.completed_at = now
        task.locked_by = ""
        task.locked_until = None
        task.run_after = None
        task.last_error = task.last_error or "작업 lock 만료 후 최대 재시도 횟수를 초과했습니다."
