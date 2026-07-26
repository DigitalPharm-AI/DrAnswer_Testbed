from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from agent_app import trace_logging
from agent_app.jobs.readiness import agent_ops_readiness_payload
from agent_app.jobs.status import worker_status_payload
from agent_app.jobs.tasks import (
    ACTIVE_STATUSES,
    DEAD,
    async_task_by_request_id,
    async_task_observability_payload,
    async_task_rows,
    async_task_status_counts,
    dismiss_dead_async_task,
    enqueue_async_task,
    retry_dead_async_task,
)
from agent_app.security import require_internal_api_token
from agent_app.tools.backend_query import BackendQueryTools
from shared.schemas import (
    AgentAsyncAccepted,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncPushMessageRequest,
    AgentAsyncTaskActionRequest,
    DailyMedicationPattern,
    MissedDoseEventPayload,
    MultiturnChatRequest,
)

router = APIRouter()

SessionFactory = Callable[[], Session]
_session_factory_getter: Callable[[], SessionFactory] | None = None
_backend_query_tools_getter: Callable[[], BackendQueryTools | None] | None = None


def configure_session_factory(getter: Callable[[], SessionFactory]) -> None:
    global _session_factory_getter
    _session_factory_getter = getter


def configure_backend_query_tools(
    getter: Callable[[], BackendQueryTools | None],
) -> None:
    global _backend_query_tools_getter
    _backend_query_tools_getter = getter


def _session() -> Session:
    if _session_factory_getter is None:
        raise RuntimeError("agent_session_factory_not_configured")
    return _session_factory_getter()()


# Keep each synchronous SQLAlchemy session inside one worker-thread call so
# connection checkin cannot be starved by FastAPI dependency cleanup.
@router.post("/agent/async/daily-patterns", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
def async_daily_patterns(payload: DailyMedicationPattern) -> AgentAsyncAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/daily-patterns", mode="async_submit", task_type="daily_pattern")
        return _enqueue_agent_task(session, "daily_pattern", payload.model_dump(mode="json"), _request_id("daily_pattern", payload.callback_context))


@router.post("/agent/async/missed-dose-events", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
def async_missed_dose_events(payload: MissedDoseEventPayload) -> AgentAsyncAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/missed-dose-events", mode="async_submit", task_type="missed_dose")
        return _enqueue_agent_task(session, "missed_dose", payload.model_dump(mode="json"), _request_id("missed_dose", payload.callback_context))


@router.post("/agent/async/chat-continuations", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
def async_chat_continuations(payload: MultiturnChatRequest) -> AgentAsyncAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/chat-continuations", mode="async_submit", task_type="chat_continuation")
        return _enqueue_agent_task(session, "chat_continuation", payload.model_dump(mode="json"), _request_id("chat_continuation", payload.callback_context))


@router.post("/agent/async/push-messages", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
def async_push_messages(payload: AgentAsyncPushMessageRequest) -> AgentAsyncAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/push-messages", mode="async_submit", task_type="push_message")
        request_id = payload.request_id or payload.idempotency_key or f"push_message:{uuid.uuid4().hex}"
        return _enqueue_agent_task(session, "push_message", payload.model_dump(mode="json", by_alias=True), request_id)


@router.post("/agent/async/clinician-alerts", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
def async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest) -> AgentAsyncAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/clinician-alerts", mode="async_submit", task_type="clinician_alert")
        request_id = payload.request_id or payload.idempotency_key or f"clinician_alert:{uuid.uuid4().hex}"
        return _enqueue_agent_task(session, "clinician_alert", payload.model_dump(mode="json"), request_id)


@router.get("/agent/async/tasks/status", dependencies=[Depends(require_internal_api_token)])
def async_task_status() -> dict:
    with _session() as session:
        counts = async_task_status_counts(session)
        active_count = sum(counts.get(status, 0) for status in ACTIVE_STATUSES)
        return {
            "status": "ok",
            "counts": counts,
            "active_count": active_count,
            "active_statuses": sorted(ACTIVE_STATUSES),
            "workers": worker_status_payload(session),
        }


@router.get("/agent/ops/readiness", dependencies=[Depends(require_internal_api_token)])
def agent_ops_readiness(response: Response) -> dict:
    with _session() as session:
        payload = agent_ops_readiness_payload(session)
    backend_read = _backend_readiness()
    payload["backend_read"] = backend_read
    if not backend_read["ok"]:
        payload["alerts"].append(
            {
                "code": "backend_read_contract_unavailable",
                "severity": "critical",
                "message": "Backend read-only DB contract is unavailable.",
            }
        )
        payload["status"] = "critical"
        response.status_code = 503
    return payload


@router.get("/agent/async/tasks", dependencies=[Depends(require_internal_api_token)])
def async_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    with _session() as session:
        tasks = async_task_rows(session, status=status, limit=limit)
        return {
            "status": "ok",
            "count": len(tasks),
            "tasks": [async_task_observability_payload(task) for task in tasks],
        }


@router.get("/agent/async/tasks/dead", dependencies=[Depends(require_internal_api_token)])
def dead_async_tasks(
    limit: int = Query(default=50, ge=1, le=200),
) -> dict:
    with _session() as session:
        tasks = async_task_rows(session, status=DEAD, limit=limit)
        return {
            "status": "ok",
            "count": len(tasks),
            "tasks": [async_task_observability_payload(task) for task in tasks],
        }


@router.post("/agent/async/tasks/{request_id}/actions", dependencies=[Depends(require_internal_api_token)])
def async_task_action(
    request_id: str,
    payload: AgentAsyncTaskActionRequest,
) -> dict:
    with _session() as session:
        task = async_task_by_request_id(session, request_id)
        if task is None:
            raise HTTPException(status_code=404, detail="agent_async_task_not_found")
        if task.status != DEAD:
            raise HTTPException(status_code=409, detail="agent_async_task_not_dead")
        if payload.action == "retry":
            task = retry_dead_async_task(session, task, reason=payload.reason)
        else:
            task = dismiss_dead_async_task(session, task, reason=payload.reason)
        session.commit()
        return {
            "status": "ok",
            "action": payload.action,
            "task": async_task_observability_payload(task),
        }


@router.get("/agent/async/tasks/{request_id}", dependencies=[Depends(require_internal_api_token)])
def async_task_detail(request_id: str) -> dict:
    with _session() as session:
        task = async_task_by_request_id(session, request_id)
        if task is None:
            raise HTTPException(status_code=404, detail="agent_async_task_not_found")
        return {
            "status": "ok",
            "task": async_task_observability_payload(task),
        }


def _request_id(task_type: str, callback_context) -> str:
    if callback_context is not None:
        if callback_context.conversation_id:
            return f"{task_type}:conversation:{callback_context.conversation_id}"
        if callback_context.job_id is not None:
            return f"{task_type}:job:{callback_context.job_id}"
        if callback_context.notification_id is not None:
            return f"{task_type}:notification:{callback_context.notification_id}"
    return f"{task_type}:{uuid.uuid4().hex}"


def _backend_readiness() -> dict:
    if _backend_query_tools_getter is None:
        return {
            "ok": False,
            "error": "backend_query_tools_not_configured",
        }
    try:
        backend_queries = _backend_query_tools_getter()
        if backend_queries is None:
            return {
                "ok": False,
                "error": "backend_read_database_url_not_configured",
            }
        return backend_queries.verify_contract()
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
        }


def _enqueue_agent_task(session: Session, task_type: str, payload: dict, request_id: str) -> AgentAsyncAccepted:
    callback_context = payload.get("callback_context") if isinstance(payload.get("callback_context"), dict) else {}
    _task, created = enqueue_async_task(
        session,
        request_id=request_id,
        task_type=task_type,
        payload=payload,
        callback_context=callback_context,
    )
    session.commit()
    return AgentAsyncAccepted(
        request_id=request_id,
        task_type=task_type,
        status="accepted" if created else "duplicate",
        callback_expected=True,
        message="비동기 작업을 접수했습니다." if created else "이미 접수된 비동기 작업입니다.",
    )
