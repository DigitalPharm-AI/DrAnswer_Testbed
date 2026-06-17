from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from agent_app.models import AgentAsyncTask
from shared.json_utils import dump_json, parse_json_object
from shared.settings import get_settings

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
) -> tuple[AgentAsyncTask, bool]:
    existing = session.scalar(select(AgentAsyncTask).where(AgentAsyncTask.request_id == request_id))
    if existing is not None:
        return existing, False
    accepted_at = datetime.utcnow()
    task = AgentAsyncTask(
        request_id=request_id,
        task_type=task_type,
        status=PENDING,
        payload_json=json.dumps(payload, ensure_ascii=False),
        callback_context_json=dump_json(callback_context or {}),
        accepted_at=accepted_at,
        run_after=accepted_at,
        max_attempts=max_attempts or get_settings().agent_task_max_attempts,
    )
    session.add(task)
    session.flush()
    return task, True


def claim_next_async_task(
    session: Session,
    *,
    worker_id: str = "",
    visibility_timeout_seconds: int | None = None,
) -> AgentAsyncTask | None:
    now = datetime.utcnow()
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
    task.locked_until = datetime.utcnow() + timedelta(seconds=60)
    session.flush()


def mark_async_task_done(session: Session, task_id: int) -> None:
    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return
    task.status = DONE
    task.completed_at = datetime.utcnow()
    task.last_error = ""
    task.locked_by = ""
    task.locked_until = None
    task.run_after = None
    session.flush()


def mark_async_task_failed(session: Session, task_id: int, message: str) -> tuple[AgentAsyncTask | None, bool]:
    task = session.get(AgentAsyncTask, task_id)
    if task is None:
        return None, False
    now = datetime.utcnow()
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


def reset_running_async_tasks(session: Session) -> int:
    tasks = session.scalars(select(AgentAsyncTask).where(AgentAsyncTask.status.in_([RUNNING, CALLBACK_SENT]))).all()
    now = datetime.utcnow()
    for task in tasks:
        task.status = PENDING
        task.last_error = "서버 재시작 후 대기 상태로 복구되었습니다."
        task.started_at = None
        task.locked_by = ""
        task.locked_until = None
        task.run_after = now
    session.flush()
    return len(tasks)


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
    now = datetime.utcnow()
    payload_keys, payload_parse_error = _safe_payload_keys(task)
    return {
        "id": task.id,
        "request_id": task.request_id,
        "task_type": task.task_type,
        "status": task.status,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "last_error": task.last_error,
        "age_seconds": _seconds_since(task.accepted_at, now),
        "runtime_seconds": _seconds_since(task.started_at, now),
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
        "callback_context": task_callback_context(task),
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
