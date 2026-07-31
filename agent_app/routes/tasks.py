from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from agent_app import trace_logging
from agent_app.jobs.readiness import agent_ops_readiness_payload
from agent_app.jobs.status import worker_status_payload
from agent_app.jobs.daily_pattern_tasks import (
    DAILY_PATTERN_ANALYSIS_TASK,
    v13_proposal_delivery_run_after,
)
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
    task_payload,
)
from agent_app.security import (
    require_agent_sync_bearer_token,
    require_internal_api_token,
)
from agent_app.tools.backend_query import BackendQueryTools
from shared.async_v13_contracts import (
    AsyncEventAccepted,
    DailyMedicationPatternAnalysisRequest,
    MissedDoseEventRequest,
)
from shared.backend_v13_contracts import CommonErrorResponse
from shared.public_ids import new_public_id
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentInternalTaskAccepted,
    AgentAsyncPushMessageRequest,
    AgentAsyncTaskActionRequest,
)
from shared.settings import get_settings
from shared.time_utils import as_aware_utc, utc_now

router = APIRouter()
MISSED_DOSE_EVENT_PATH = "/agent/async/missed-dose-events"
DAILY_MEDICATION_PATTERN_ANALYSIS_PATH = (
    "/agent/async/daily-medication-pattern-analysis"
)

SessionFactory = Callable[[], Session]
_session_factory_getter: Callable[[], SessionFactory] | None = None
_backend_query_tools_getter: Callable[[], BackendQueryTools | None] | None = None
_MEDICATION_EVENT_ENQUEUE_LOCKS = tuple(Lock() for _ in range(64))


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
@router.post(
    MISSED_DOSE_EVENT_PATH,
    response_model=AsyncEventAccepted,
    status_code=202,
    dependencies=[Depends(require_agent_sync_bearer_token)],
    responses={
        200: {
            "model": AsyncEventAccepted,
            "description": "The same request was already accepted.",
        },
        400: {"model": CommonErrorResponse},
        401: {"model": CommonErrorResponse},
        409: {"model": CommonErrorResponse},
        500: {"model": CommonErrorResponse},
        503: {"model": CommonErrorResponse},
    },
)
def async_missed_dose_events(
    request: MissedDoseEventRequest,
    response: Response,
) -> AsyncEventAccepted:
    with _session() as session:
        trace_logging.log_info(
            "agent_api_call",
            path=MISSED_DOSE_EVENT_PATH,
            mode="async_submit",
            task_type="missed_dose",
        )
        try:
            # Serialize equal request-id stripes locally; additional replicas
            # remain protected by the PostgreSQL unique key.
            with _medication_event_enqueue_lock(request.request_id):
                accepted = _enqueue_missed_dose_event(session, request)
                if accepted.status == "duplicate":
                    response.status_code = 200
                return accepted
        except HTTPException:
            raise
        except Exception as exc:
            session.rollback()
            trace_logging.log_info(
                "agent_async_queue_submit_failed",
                path=MISSED_DOSE_EVENT_PATH,
                task_type="missed_dose",
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "QUEUE_UNAVAILABLE",
                    "message": (
                        "The asynchronous processing queue is unavailable."
                    ),
                    "retryable": True,
                    "details": None,
                },
            ) from exc


@router.post(
    DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    response_model=AsyncEventAccepted,
    status_code=202,
    dependencies=[Depends(require_agent_sync_bearer_token)],
    responses={
        200: {
            "model": AsyncEventAccepted,
            "description": "The same request was already accepted.",
        },
        400: {"model": CommonErrorResponse},
        401: {"model": CommonErrorResponse},
        409: {"model": CommonErrorResponse},
        500: {"model": CommonErrorResponse},
        503: {"model": CommonErrorResponse},
    },
)
def async_daily_medication_pattern_analysis(
    request: DailyMedicationPatternAnalysisRequest,
    response: Response,
) -> AsyncEventAccepted:
    with _session() as session:
        trace_logging.log_info(
            "agent_api_call",
            path=DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
            mode="async_submit",
            task_type=DAILY_PATTERN_ANALYSIS_TASK,
        )
        try:
            with _medication_event_enqueue_lock(request.request_id):
                accepted = _enqueue_daily_pattern_analysis(
                    session,
                    request,
                )
                if accepted.status == "duplicate":
                    response.status_code = 200
                return accepted
        except HTTPException:
            raise
        except Exception as exc:
            session.rollback()
            trace_logging.log_info(
                "agent_async_queue_submit_failed",
                path=DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
                task_type=DAILY_PATTERN_ANALYSIS_TASK,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "QUEUE_UNAVAILABLE",
                    "message": (
                        "The asynchronous processing queue is unavailable."
                    ),
                    "retryable": True,
                    "details": None,
                },
            ) from exc


@router.post("/agent/async/push-messages", response_model=AgentInternalTaskAccepted, dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
def async_push_messages(payload: AgentAsyncPushMessageRequest) -> AgentInternalTaskAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/push-messages", mode="async_submit", task_type="push_message")
        request_id = payload.request_id or payload.idempotency_key or f"push_message:{uuid.uuid4().hex}"
        return _enqueue_agent_task(session, "push_message", payload.model_dump(mode="json", by_alias=True), request_id)


@router.post("/agent/async/clinician-alerts", response_model=AgentInternalTaskAccepted, dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
def async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest) -> AgentInternalTaskAccepted:
    with _session() as session:
        trace_logging.log_info("agent_api_call", path="/agent/async/clinician-alerts", mode="async_submit", task_type="clinician_alert")
        request_id = payload.request_id or payload.idempotency_key or f"clinician_alert:{uuid.uuid4().hex}"
        return _enqueue_agent_task(session, "clinician_alert", payload.model_dump(mode="json"), request_id)


@router.get("/agent/async/tasks/status", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


@router.get("/agent/ops/readiness", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


@router.get("/agent/async/tasks", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


@router.get("/agent/async/tasks/dead", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


@router.post("/agent/async/tasks/{request_id}/actions", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


@router.get("/agent/async/tasks/{request_id}", dependencies=[Depends(require_internal_api_token)], include_in_schema=False)
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


def _enqueue_agent_task(
    session: Session,
    task_type: str,
    payload: dict,
    request_id: str,
) -> AgentInternalTaskAccepted:
    callback_context = payload.get("callback_context") if isinstance(payload.get("callback_context"), dict) else {}
    task, created = enqueue_async_task(
        session,
        request_id=request_id,
        task_type=task_type,
        payload=payload,
        callback_context=callback_context,
    )
    session.commit()
    return AgentInternalTaskAccepted(
        request_id=request_id,
        task_type=task_type,
        status="accepted" if created else "duplicate",
        accepted_at=as_aware_utc(task.accepted_at),
    )


def _enqueue_missed_dose_event(
    session: Session,
    request: MissedDoseEventRequest,
) -> AsyncEventAccepted:
    request_hash = _contract_request_hash(request)
    payload = request.model_dump(mode="json")
    payload["_contract_request_hash"] = request_hash
    task, created = enqueue_async_task(
        session,
        request_id=request.request_id,
        task_type="missed_dose",
        payload=payload,
        callback_context={},
    )
    if not created:
        existing_payload = task_payload(task)
        if (
            task.task_type != "missed_dose"
            or existing_payload.get("_contract_request_hash") != request_hash
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": (
                        "The request_id was reused with a different "
                        "request body."
                    ),
                    "retryable": False,
                    "details": {"request_id": request.request_id},
                },
            )
        return AsyncEventAccepted(
            request_id=request.request_id,
            status="duplicate",
        )

    session.commit()
    return AsyncEventAccepted(
        request_id=request.request_id,
        status="accepted",
    )


def _enqueue_daily_pattern_analysis(
    session: Session,
    request: DailyMedicationPatternAnalysisRequest,
) -> AsyncEventAccepted:
    request_hash = _contract_request_hash(request)
    analysis_date = request.analysis_date.isoformat()
    delivery_run_after = v13_proposal_delivery_run_after(
        received_at=utc_now(),
    ).isoformat()
    callback_context = {
        "app_base_url": get_settings().system_base_url,
    }
    for index, patient_id in enumerate(request.patient_id):
        task_request_id = (
            request.request_id
            if index == 0
            else new_public_id("request")
        )
        payload = {
            "request_id": request.request_id,
            "patient_id": patient_id,
            "analysis_date": analysis_date,
            "delivery_run_after": delivery_run_after,
            "_batch_patient_id": request.patient_id,
            "_contract_request_hash": request_hash,
        }
        task, created = enqueue_async_task(
            session,
            request_id=task_request_id,
            task_type=DAILY_PATTERN_ANALYSIS_TASK,
            payload=payload,
            callback_context=callback_context,
        )
        if index != 0 or created:
            continue
        existing_payload = task_payload(task)
        if (
            task.task_type != DAILY_PATTERN_ANALYSIS_TASK
            or existing_payload.get("_contract_request_hash")
            != request_hash
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "IDEMPOTENCY_CONFLICT",
                    "message": (
                        "The request_id was reused with a different "
                        "request body."
                    ),
                    "retryable": False,
                    "details": {"request_id": request.request_id},
                },
            )
        return AsyncEventAccepted(
            request_id=request.request_id,
            status="duplicate",
        )

    session.commit()
    return AsyncEventAccepted(
        request_id=request.request_id,
        status="accepted",
    )


def _medication_event_enqueue_lock(request_id: str) -> Lock:
    digest = hashlib.sha256(request_id.encode("utf-8")).digest()
    return _MEDICATION_EVENT_ENQUEUE_LOCKS[
        digest[0] % len(_MEDICATION_EVENT_ENQUEUE_LOCKS)
    ]


def _contract_request_hash(
    request: (
        MissedDoseEventRequest
        | DailyMedicationPatternAnalysisRequest
    ),
) -> str:
    encoded = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
