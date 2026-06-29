from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_app.models import AgentWorkerHeartbeat
from shared.redaction import redacted_clinical_text_label
from shared.settings import get_settings
from shared.time_utils import utc_now

WORKER_RUNNING = "running"
WORKER_STOPPED = "stopped"
WORKER_STALE = "stale"


def mark_worker_started(session: Session, worker_id: str) -> AgentWorkerHeartbeat:
    now = utc_now()
    row = _worker_row(session, worker_id)
    if row is None:
        row = AgentWorkerHeartbeat(worker_id=worker_id)
        session.add(row)
    row.status = WORKER_RUNNING
    row.started_at = now
    row.heartbeat_at = now
    row.stopped_at = None
    row.current_task_request_id = ""
    row.current_task_type = ""
    row.processed_count = 0
    row.last_error = ""
    session.flush()
    return row


def record_worker_heartbeat(
    session: Session,
    worker_id: str,
    *,
    current_task_request_id: str = "",
    current_task_type: str = "",
    last_error: str = "",
) -> AgentWorkerHeartbeat:
    now = utc_now()
    row = _worker_row(session, worker_id)
    if row is None:
        row = AgentWorkerHeartbeat(worker_id=worker_id, started_at=now)
        session.add(row)
    row.status = WORKER_RUNNING
    row.heartbeat_at = now
    row.stopped_at = None
    row.current_task_request_id = current_task_request_id
    row.current_task_type = current_task_type
    row.last_error = last_error
    session.flush()
    return row


def record_worker_task_completed(session: Session, worker_id: str) -> AgentWorkerHeartbeat:
    row = record_worker_heartbeat(session, worker_id)
    row.processed_count += 1
    row.current_task_request_id = ""
    row.current_task_type = ""
    session.flush()
    return row


def mark_worker_stopped(session: Session, worker_id: str) -> AgentWorkerHeartbeat:
    now = utc_now()
    row = _worker_row(session, worker_id)
    if row is None:
        row = AgentWorkerHeartbeat(worker_id=worker_id, started_at=now)
        session.add(row)
    row.status = WORKER_STOPPED
    row.heartbeat_at = now
    row.stopped_at = now
    row.current_task_request_id = ""
    row.current_task_type = ""
    session.flush()
    return row


def worker_status_payload(session: Session) -> list[dict[str, Any]]:
    settings = get_settings()
    stale_cutoff = utc_now() - timedelta(seconds=settings.agent_worker_stale_after_seconds)
    rows = session.scalars(select(AgentWorkerHeartbeat).order_by(AgentWorkerHeartbeat.heartbeat_at.desc())).all()
    return [_serialize_worker(row, stale_cutoff) for row in rows]


def _worker_row(session: Session, worker_id: str) -> AgentWorkerHeartbeat | None:
    return session.scalar(select(AgentWorkerHeartbeat).where(AgentWorkerHeartbeat.worker_id == worker_id))


def _serialize_worker(row: AgentWorkerHeartbeat, stale_cutoff: datetime) -> dict[str, Any]:
    effective_status = row.status
    if row.status == WORKER_RUNNING and row.heartbeat_at < stale_cutoff:
        effective_status = WORKER_STALE
    return {
        "worker_id": row.worker_id,
        "status": effective_status,
        "stored_status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "heartbeat_at": row.heartbeat_at.isoformat() if row.heartbeat_at else None,
        "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
        "current_task_request_id": row.current_task_request_id,
        "current_task_type": row.current_task_type,
        "processed_count": row.processed_count,
        "last_error": _safe_last_error(row.last_error),
    }


def _safe_last_error(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if "clinical text redacted" in text:
        return text
    if len(text) <= 80 and all(char.isascii() and (char.isalnum() or char in "_:-.") for char in text):
        return text
    return redacted_clinical_text_label(text, key="last_error")
