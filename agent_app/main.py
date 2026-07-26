from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent_app.errors import AgentExecutionError
from agent_app.integration.chat_contracts import chat_error
from agent_app.jobs.tasks import reset_running_async_tasks
from agent_app.jobs.worker import async_task_worker
from agent_app.openapi_v12 import install_agent_v12_openapi
from agent_app.persistence.db import SessionLocal, engine
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import Base
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.readiness import collect_agent_service_readiness
from agent_app.routes import chat as chat_routes
from agent_app.routes import feedback as feedback_routes
from agent_app.routes import mcp as mcp_routes
from agent_app.routes import tasks as task_routes
from agent_app.routes.chat import multiturn_chat, resolve_mutation_confirmation, sync_chat
from agent_app.routes.feedback import chat_feedback
from agent_app.routes.mcp import agent_mcp
from agent_app.routes.model import agent_model_config, router as model_router, update_agent_model_config
from agent_app.routes.tasks import (
    _enqueue_agent_task,
    _request_id,
    agent_ops_readiness,
    async_chat_continuations,
    async_clinician_alerts,
    async_daily_patterns,
    async_missed_dose_events,
    async_push_messages,
    async_task_action,
    async_task_detail,
    async_task_status,
    async_tasks,
    dead_async_tasks,
)
from agent_app.runtime import create_runtime_components
from shared.redaction import safe_exception_summary
from shared.settings import get_settings

runtime_components = create_runtime_components()
mcp_tool_server = runtime_components.tool_server
orchestrator = runtime_components.orchestrator
logger = logging.getLogger("uvicorn.error")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

chat_routes.configure_orchestrator(lambda: orchestrator)
chat_routes.configure_sync_session_factory(lambda: SessionLocal)
chat_routes.configure_backend_query_tools(lambda: mcp_tool_server.backend_queries)
feedback_routes.configure_feedback_session_factory(lambda: SessionLocal)
feedback_routes.configure_feedback_backend_query_tools(
    lambda: mcp_tool_server.backend_queries
)
mcp_routes.configure_mcp_server(lambda: mcp_tool_server)
task_routes.configure_session_factory(lambda: SessionLocal)
task_routes.configure_backend_query_tools(lambda: mcp_tool_server.backend_queries)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.require_internal_api_token_in_production()
    settings.require_agent_sync_api_token_in_production()
    settings.require_backend_api_token_in_production()
    settings.require_backend_read_database_url_in_production()
    settings.require_agent_feedback_encryption()
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    retention_result = purge_expired_agent_state(SessionLocal)
    if any(retention_result.values()):
        logger.info("agent_state_retention_cleanup result=%s", retention_result)
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
app.include_router(model_router)
app.include_router(chat_routes.router)
app.include_router(feedback_routes.router)
app.include_router(mcp_routes.router)
app.include_router(task_routes.router)
install_agent_v12_openapi(app)


@app.exception_handler(AgentExecutionError)
async def agent_execution_error_handler(_: Request, exc: AgentExecutionError) -> JSONResponse:
    safe_message = safe_exception_summary(exc)
    logger.warning(
        "agent_app_execution_failed trace_id=%s agent=%s decision=%s error_type=%s message=%s",
        exc.trace_id,
        exc.agent_name,
        exc.decision_type,
        exc.error_type,
        safe_message,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error_type": exc.error_type,
            "trace_id": exc.trace_id,
            "agent_name": exc.agent_name,
            "decision_type": exc.decision_type,
            "message": safe_message,
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path not in {
        chat_routes.SYNC_CHAT_PATH,
        feedback_routes.FEEDBACK_API_PATH,
    }:
        return await request_validation_exception_handler(request, exc)
    details = {
        "errors": [
            {
                "location": [str(value) for value in error.get("loc", ())],
                "message": str(error.get("msg") or "invalid value"),
                "type": str(error.get("type") or "validation_error"),
            }
            for error in exc.errors()
        ]
    }
    body = chat_error(
        "INVALID_REQUEST",
        "Request schema or required field is invalid.",
        retryable=False,
        details=details,
    ).model_dump(mode="json")
    return JSONResponse(status_code=400, content=body)


@app.exception_handler(HTTPException)
async def http_exception_contract_handler(request: Request, exc: HTTPException):
    if request.url.path not in {
        chat_routes.SYNC_CHAT_PATH,
        feedback_routes.FEEDBACK_API_PATH,
    }:
        return await http_exception_handler(request, exc)
    if exc.status_code == 401:
        body = chat_error(
            "UNAUTHORIZED",
            "Authorization failed.",
            retryable=False,
            details=None,
        ).model_dump(mode="json")
        return JSONResponse(status_code=401, content=body, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.get("/health")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok", "runtime": "langgraph_native"}


@app.get("/health/ready")
async def readinesscheck() -> JSONResponse:
    payload = await asyncio.to_thread(
        collect_agent_service_readiness,
        agent_engine=engine,
        backend_queries=mcp_tool_server.backend_queries,
        settings=get_settings(),
    )
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=payload)


__all__ = [
    "_enqueue_agent_task",
    "_request_id",
    "agent_execution_error_handler",
    "agent_mcp",
    "agent_model_config",
    "agent_ops_readiness",
    "app",
    "chat_feedback",
    "async_chat_continuations",
    "async_clinician_alerts",
    "async_daily_patterns",
    "async_missed_dose_events",
    "async_push_messages",
    "async_task_action",
    "async_task_detail",
    "async_task_status",
    "async_tasks",
    "dead_async_tasks",
    "healthcheck",
    "http_exception_contract_handler",
    "lifespan",
    "multiturn_chat",
    "request_validation_error_handler",
    "readinesscheck",
    "resolve_mutation_confirmation",
    "sync_chat",
    "update_agent_model_config",
]
