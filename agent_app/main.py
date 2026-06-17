from __future__ import annotations

import contextlib
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent_app import trace_logging
from agent_app.async_tasks import (
    ACTIVE_STATUSES,
    DEAD,
    async_task_by_request_id,
    async_task_observability_payload,
    async_task_rows,
    async_task_status_counts,
    enqueue_async_task,
    reset_running_async_tasks,
)
from agent_app.async_worker import async_task_worker
from agent_app.db import engine, get_session
from agent_app.errors import AgentExecutionError
from agent_app.migrations import run_migrations
from agent_app.models import Base
from agent_app.providers import describe_model_config, set_runtime_model_tier
from agent_app.runtime import create_runtime_components
from agent_app.security import require_internal_api_token
from agent_app.worker_status import worker_status_payload
from shared.schemas import (
    AgentAsyncAccepted,
    AgentAsyncClinicianAlertRequest,
    AgentAsyncPushMessageRequest,
    AgentModelConfig,
    AgentModelTierRequest,
    AgentResponse,
    DailyMedicationPattern,
    MissedDoseEventPayload,
    MultiturnChatRequest,
)
from shared.settings import get_settings

runtime_components = create_runtime_components()
mcp_tool_server = runtime_components.tool_server
orchestrator = runtime_components.orchestrator
logger = logging.getLogger("uvicorn.error")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.require_internal_api_token_in_production()
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    stop_event = threading.Event()
    worker_thread: threading.Thread | None = None
    if settings.agent_embedded_worker_enabled:
        with Session(engine) as session:
            reset_running_async_tasks(session)
            session.commit()
        logger.warning("agent_embedded_worker_enabled=true; use python -m agent_app.worker_main for production separation.")
        worker_thread = threading.Thread(target=async_task_worker, args=(stop_event, orchestrator), name="agent-async-task-worker", daemon=True)
        worker_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        if worker_thread is not None:
            with contextlib.suppress(RuntimeError):
                worker_thread.join(timeout=2)


app = FastAPI(title="Medication Reminder Agent LangGraph Native", lifespan=lifespan)


@app.exception_handler(AgentExecutionError)
async def agent_execution_error_handler(_: Request, exc: AgentExecutionError) -> JSONResponse:
    logger.warning(
        "agent_app_execution_failed trace_id=%s agent=%s decision=%s error_type=%s message=%s",
        exc.trace_id,
        exc.agent_name,
        exc.decision_type,
        exc.error_type,
        exc.message,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_type": exc.error_type,
            "trace_id": exc.trace_id,
            "agent_name": exc.agent_name,
            "decision_type": exc.decision_type,
            "message": exc.message,
        },
    )


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok", "runtime": "langgraph_native"}


@app.get("/agent/model-config", response_model=AgentModelConfig, dependencies=[Depends(require_internal_api_token)])
async def agent_model_config() -> AgentModelConfig:
    return describe_model_config()


@app.post("/agent/model-config", response_model=AgentModelConfig, dependencies=[Depends(require_internal_api_token)])
async def update_agent_model_config(payload: AgentModelTierRequest) -> AgentModelConfig:
    return set_runtime_model_tier(payload.model_tier)


@app.post("/agent/multiturn-chat", response_model=AgentResponse, dependencies=[Depends(require_internal_api_token)])
async def multiturn_chat(payload: MultiturnChatRequest) -> AgentResponse:
    trace_logging.log_info("agent_api_call", path="/agent/multiturn-chat", mode="sync", task_type="multiturn_chat")
    return await orchestrator.invoke("multiturn_chat", payload.model_dump(mode="json"))


@app.post("/agent/async/daily-patterns", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
async def async_daily_patterns(payload: DailyMedicationPattern, session: Session = Depends(get_session)) -> AgentAsyncAccepted:
    trace_logging.log_info("agent_api_call", path="/agent/async/daily-patterns", mode="async_submit", task_type="daily_pattern")
    return _enqueue_agent_task(session, "daily_pattern", payload.model_dump(mode="json"), _request_id("daily_pattern", payload.callback_context))


@app.post("/agent/async/missed-dose-events", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
async def async_missed_dose_events(payload: MissedDoseEventPayload, session: Session = Depends(get_session)) -> AgentAsyncAccepted:
    trace_logging.log_info("agent_api_call", path="/agent/async/missed-dose-events", mode="async_submit", task_type="missed_dose")
    return _enqueue_agent_task(session, "missed_dose", payload.model_dump(mode="json"), _request_id("missed_dose", payload.callback_context))


@app.post("/agent/async/chat-continuations", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
async def async_chat_continuations(payload: MultiturnChatRequest, session: Session = Depends(get_session)) -> AgentAsyncAccepted:
    trace_logging.log_info("agent_api_call", path="/agent/async/chat-continuations", mode="async_submit", task_type="chat_continuation")
    return _enqueue_agent_task(session, "chat_continuation", payload.model_dump(mode="json"), _request_id("chat_continuation", payload.callback_context))


@app.post("/agent/async/push-messages", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
async def async_push_messages(payload: AgentAsyncPushMessageRequest, session: Session = Depends(get_session)) -> AgentAsyncAccepted:
    trace_logging.log_info("agent_api_call", path="/agent/async/push-messages", mode="async_submit", task_type="push_message")
    request_id = payload.request_id or payload.idempotency_key or f"push_message:{uuid.uuid4().hex}"
    return _enqueue_agent_task(session, "push_message", payload.model_dump(mode="json", by_alias=True), request_id)


@app.post("/agent/async/clinician-alerts", response_model=AgentAsyncAccepted, dependencies=[Depends(require_internal_api_token)])
async def async_clinician_alerts(payload: AgentAsyncClinicianAlertRequest, session: Session = Depends(get_session)) -> AgentAsyncAccepted:
    trace_logging.log_info("agent_api_call", path="/agent/async/clinician-alerts", mode="async_submit", task_type="clinician_alert")
    request_id = payload.request_id or payload.idempotency_key or f"clinician_alert:{uuid.uuid4().hex}"
    return _enqueue_agent_task(session, "clinician_alert", payload.model_dump(mode="json"), request_id)


@app.post("/agent/mcp", dependencies=[Depends(require_internal_api_token)])
async def agent_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    context_payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    return await mcp_tool_server.handle_json_rpc(
        payload,
        trace_id=str(meta.get("trace_id") or f"mcp:{uuid.uuid4().hex}"),
        source_event_type=str(meta.get("source_event_type") or "mcp"),
        payload=context_payload,
    )


@app.get("/agent/async/tasks/status", dependencies=[Depends(require_internal_api_token)])
async def async_task_status(session: Session = Depends(get_session)) -> dict:
    counts = async_task_status_counts(session)
    active_count = sum(counts.get(status, 0) for status in ACTIVE_STATUSES)
    return {
        "status": "ok",
        "counts": counts,
        "active_count": active_count,
        "active_statuses": sorted(ACTIVE_STATUSES),
        "workers": worker_status_payload(session),
    }


@app.get("/agent/async/tasks", dependencies=[Depends(require_internal_api_token)])
async def async_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    tasks = async_task_rows(session, status=status, limit=limit)
    return {
        "status": "ok",
        "count": len(tasks),
        "tasks": [async_task_observability_payload(task) for task in tasks],
    }


@app.get("/agent/async/tasks/dead", dependencies=[Depends(require_internal_api_token)])
async def dead_async_tasks(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
) -> dict:
    tasks = async_task_rows(session, status=DEAD, limit=limit)
    return {
        "status": "ok",
        "count": len(tasks),
        "tasks": [async_task_observability_payload(task) for task in tasks],
    }


@app.get("/agent/async/tasks/{request_id}", dependencies=[Depends(require_internal_api_token)])
async def async_task_detail(request_id: str, session: Session = Depends(get_session)) -> dict:
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
        if callback_context.conversation_id:
            return f"{task_type}:conversation:{callback_context.conversation_id}"
        if callback_context.notification_id is not None:
            return f"{task_type}:notification:{callback_context.notification_id}"
    return f"{task_type}:{uuid.uuid4().hex}"


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
