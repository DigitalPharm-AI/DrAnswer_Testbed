from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_app.jobs.status import WORKER_RUNNING, WORKER_STALE, worker_status_payload
from agent_app.jobs.tasks import ACTIVE_STATUSES, DEAD, PENDING, async_task_rows, async_task_status_counts
from agent_app.persistence.models import AgentAsyncTask
from shared.redaction import redact_inline_secrets
from shared.settings import get_settings
from shared.time_utils import utc_now

RUNBOOK_PATH = "docs/PRODUCTION_READINESS.md#incident-runbook"


def agent_ops_readiness_payload(session: Session) -> dict[str, Any]:
    settings = get_settings()
    counts = async_task_status_counts(session)
    workers = worker_status_payload(session)
    running_worker_count = sum(1 for worker in workers if worker.get("status") == WORKER_RUNNING)
    stale_worker_count = sum(1 for worker in workers if worker.get("status") == WORKER_STALE)
    active_count = sum(counts.get(status, 0) for status in ACTIVE_STATUSES)
    pending_count = counts.get(PENDING, 0)
    dead_count = counts.get(DEAD, 0)
    oldest_pending_age = _oldest_age_seconds(session, [PENDING])
    oldest_active_age = _oldest_age_seconds(session, list(ACTIVE_STATUSES))
    dead_tasks = async_task_rows(session, status=DEAD, limit=200)
    callback_failure_count = sum(1 for task in dead_tasks if _looks_like_callback_failure(task.last_error))
    provider_failure_count = sum(1 for task in dead_tasks if _looks_like_provider_failure(task.last_error))

    alerts = _readiness_alerts(
        active_count=active_count,
        pending_count=pending_count,
        dead_count=dead_count,
        running_worker_count=running_worker_count,
        stale_worker_count=stale_worker_count,
        worker_count=len(workers),
        oldest_pending_age=oldest_pending_age,
        pending_age_warn_seconds=max(60, settings.agent_task_visibility_timeout_seconds),
        callback_failure_count=callback_failure_count,
        provider_failure_count=provider_failure_count,
    )
    status = _overall_status(alerts)
    return {
        "status": status,
        "alerts": alerts,
        "metrics": {
            "active_count": active_count,
            "pending_count": pending_count,
            "dead_count": dead_count,
            "worker_count": len(workers),
            "running_worker_count": running_worker_count,
            "stale_worker_count": stale_worker_count,
            "oldest_pending_age_seconds": oldest_pending_age,
            "oldest_active_age_seconds": oldest_active_age,
            "callback_failure_count": callback_failure_count,
            "provider_failure_count": provider_failure_count,
        },
        "counts": counts,
        "workers": workers,
        "dead_task_samples": [_dead_task_sample(task) for task in dead_tasks[:5]],
        "thresholds": {
            "pending_age_warn_seconds": max(60, settings.agent_task_visibility_timeout_seconds),
            "worker_stale_after_seconds": settings.agent_worker_stale_after_seconds,
            "dead_task_critical_count": 1,
        },
        "runbook": RUNBOOK_PATH,
    }


def _readiness_alerts(
    *,
    active_count: int,
    pending_count: int,
    dead_count: int,
    running_worker_count: int,
    stale_worker_count: int,
    worker_count: int,
    oldest_pending_age: int | None,
    pending_age_warn_seconds: int,
    callback_failure_count: int,
    provider_failure_count: int,
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    if active_count and running_worker_count == 0:
        alerts.append(_alert("agent_async_worker_unavailable", "critical", "Active async tasks exist but no running worker heartbeat is available."))
    elif worker_count == 0:
        alerts.append(_alert("agent_async_worker_not_reporting", "warning", "No worker heartbeat has been recorded yet."))
    if stale_worker_count:
        alerts.append(_alert("agent_async_worker_stale", "warning", "At least one worker heartbeat is stale.", count=stale_worker_count))
    if pending_count and oldest_pending_age is not None and oldest_pending_age >= pending_age_warn_seconds:
        alerts.append(
            _alert(
                "agent_async_pending_age_exceeded",
                "warning",
                "The oldest pending async task exceeded the pending-age warning threshold.",
                age_seconds=oldest_pending_age,
            )
        )
    if dead_count:
        alerts.append(_alert("agent_async_dead_tasks_present", "critical", "Dead async tasks require operator review.", count=dead_count))
    if callback_failure_count:
        alerts.append(_alert("agent_async_callback_failures_detected", "critical", "Dead tasks include callback delivery failures.", count=callback_failure_count))
    if provider_failure_count:
        alerts.append(_alert("agent_provider_failures_detected", "warning", "Dead tasks include model/provider execution failures.", count=provider_failure_count))
    return alerts


def _overall_status(alerts: list[dict[str, Any]]) -> str:
    severities = {str(alert.get("severity")) for alert in alerts}
    if "critical" in severities:
        return "critical"
    if alerts:
        return "degraded"
    return "ok"


def _alert(code: str, severity: str, message: str, **details: Any) -> dict[str, Any]:
    payload = {
        "code": code,
        "severity": severity,
        "message": message,
    }
    payload.update(details)
    return payload


def _oldest_age_seconds(session: Session, statuses: list[str]) -> int | None:
    accepted_at = session.execute(
        select(func.min(AgentAsyncTask.accepted_at)).where(AgentAsyncTask.status.in_(statuses))
    ).scalar()
    if accepted_at is None:
        return None
    return max(0, int((utc_now() - accepted_at).total_seconds()))


def _looks_like_callback_failure(message: str | None) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in ("callback", "connecterror", "httperror", "httpstatuserror", "readtimeout", "writeerror"))


def _looks_like_provider_failure(message: str | None) -> bool:
    text = (message or "").lower()
    return any(marker in text for marker in ("bedrock", "provider", "llm", "model", "agentexecutionerror", "validation"))


def _dead_task_sample(task: AgentAsyncTask) -> dict[str, Any]:
    return {
        "request_id": task.request_id,
        "task_type": task.task_type,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "last_error": redact_inline_secrets(task.last_error or ""),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
