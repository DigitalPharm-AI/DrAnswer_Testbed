from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from agent_app.errors import AgentExecutionError, public_processing_error
from agent_app.jobs.tasks import reset_running_async_tasks
from agent_app.jobs.worker import async_task_worker
from agent_app.openapi_v13 import install_agent_v13_openapi
from agent_app.persistence.db import SessionLocal, engine
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.persistence.schema import verify_agent_schema_current
from agent_app.readiness import collect_agent_service_readiness
from agent_app.routes import chat as chat_routes
from agent_app.routes import feedback as feedback_routes
from agent_app.routes import mcp as mcp_routes
from agent_app.routes import tasks as task_routes
from agent_app.routes.chat import sync_chat
from agent_app.routes.feedback import chat_feedback
from agent_app.routes.mcp import agent_mcp
from agent_app.routes.model import agent_model_config, update_agent_model_config
from agent_app.routes.model import router as model_router
from agent_app.routes.tasks import (
    DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    MISSED_DOSE_EVENT_PATH,
    _enqueue_agent_task,
    _request_id,
    agent_ops_readiness,
    async_clinician_alerts,
    async_missed_dose_events,
    async_push_messages,
    async_task_action,
    async_task_detail,
    async_task_status,
    async_tasks,
    dead_async_tasks,
)
from agent_app.runtime import create_runtime_components
from agent_app.security import require_agent_sync_bearer_token
from shared.backend_v13_contracts import CommonErrorResponse, ContractError
from shared.chat_contracts import chat_error
from shared.public_ids import request_id_from_body, request_id_from_request
from shared.redaction import safe_exception_summary
from shared.settings import get_settings

runtime_components = create_runtime_components()
mcp_tool_server = runtime_components.tool_server
orchestrator = runtime_components.orchestrator
logger = logging.getLogger("uvicorn.error")
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
ASYNC_EVENT_ACCEPTANCE_PATHS = frozenset(
    {
        MISSED_DOSE_EVENT_PATH,
        DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
    }
)


async def _periodic_retention_cleanup(interval_seconds: int) -> None:
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(
                purge_expired_agent_state,
                SessionLocal,
            )
            if any(result.values()):
                logger.info(
                    "agent_state_retention_cleanup result=%s",
                    result,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "agent_state_retention_cleanup_failed error=%s",
                safe_exception_summary(exc),
            )

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
    settings.require_internal_api_token()
    settings.require_service_api_token()
    settings.require_backend_service_https()
    settings.require_backend_read_database_url()
    settings.require_agent_postgresql()
    settings.require_backend_read_postgresql()
    settings.require_agent_feedback_encryption()
    verify_agent_schema_current(engine)
    retention_result = purge_expired_agent_state(SessionLocal)
    if any(retention_result.values()):
        logger.info("agent_state_retention_cleanup result=%s", retention_result)
    retention_task = asyncio.create_task(
        _periodic_retention_cleanup(
            settings.agent_retention_cleanup_interval_seconds
        ),
        name="agent-state-retention-cleanup",
    )
    stop_event = threading.Event()
    worker_thread: threading.Thread | None = None
    if settings.agent_embedded_worker_enabled:
        with Session(engine) as session:
            reset_running_async_tasks(session)
            session.commit()
        logger.warning("agent_embedded_worker_enabled=true; use python -m agent_app.worker_main for production separation.")
        worker_thread = threading.Thread(
            target=async_task_worker,
            args=(stop_event, orchestrator, mcp_tool_server.backend_queries),
            name="agent-async-task-worker",
            daemon=True,
        )
        worker_thread.start()
    try:
        yield
    finally:
        retention_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retention_task
        stop_event.set()
        if worker_thread is not None:
            with contextlib.suppress(RuntimeError):
                worker_thread.join(timeout=2)


app = FastAPI(
    title="Medication Reminder Agent LangGraph Native",
    version="1.3",
    lifespan=lifespan,
)
app.include_router(model_router, include_in_schema=False)
app.include_router(chat_routes.router)
app.include_router(feedback_routes.router)
app.include_router(mcp_routes.router, include_in_schema=False)
app.include_router(task_routes.router)
install_agent_v13_openapi(app)


@app.exception_handler(AgentExecutionError)
async def agent_execution_error_handler(
    request: Request,
    exc: AgentExecutionError,
) -> JSONResponse:
    safe_message = safe_exception_summary(exc)
    public_error = public_processing_error(exc)
    logger.warning(
        "agent_app_execution_failed trace_id=%s agent=%s decision=%s error_type=%s message=%s",
        exc.trace_id,
        exc.agent_name,
        exc.decision_type,
        exc.error_type,
        safe_message,
    )
    body = CommonErrorResponse(
        request_id=await request_id_from_request(request),
        error=ContractError(
            code=public_error.code,
            message=public_error.message,
            retryable=public_error.retryable,
            details=None,
        ),
    )
    return JSONResponse(
        status_code=500,
        content=body.model_dump(mode="json"),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    if request.url.path in ASYNC_EVENT_ACCEPTANCE_PATHS:
        body = CommonErrorResponse(
            request_id=request_id_from_body(exc.body),
            error=ContractError(
                code="INVALID_REQUEST",
                message="Request schema or required field is invalid.",
                retryable=False,
                details={
                    "violations": [
                        {
                            "location": [
                                str(value)
                                for value in error.get("loc", ())
                            ],
                            "message": str(
                                error.get("msg") or "invalid value",
                            ),
                            "type": str(
                                error.get("type")
                                or "validation_error",
                            ),
                        }
                        for error in exc.errors()
                    ]
                },
            ),
        )
        return JSONResponse(
            status_code=400,
            content=body.model_dump(mode="json"),
        )
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
        request_id=request_id_from_body(exc.body),
        retryable=False,
        details=details,
    ).model_dump(mode="json")
    return JSONResponse(status_code=400, content=body)


@app.exception_handler(HTTPException)
async def http_exception_contract_handler(request: Request, exc: HTTPException):
    if request.url.path in ASYNC_EVENT_ACCEPTANCE_PATHS:
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        body = CommonErrorResponse(
            request_id=await request_id_from_request(request),
            error=ContractError(
                code=str(
                    detail.get("code")
                    or (
                        "UNAUTHORIZED"
                        if exc.status_code == 401
                        else "AI_PROCESSING_ERROR"
                    )
                ),
                message=str(
                    detail.get("message")
                    or (
                        "Authorization failed."
                        if exc.status_code == 401
                        else (
                            "An internal AI Server processing error "
                            "occurred."
                        )
                    )
                ),
                retryable=bool(detail.get("retryable", False)),
                details=(
                    detail.get("details")
                    if isinstance(detail.get("details"), dict)
                    else None
                ),
            ),
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=exc.headers,
        )
    if request.url.path not in {
        chat_routes.SYNC_CHAT_PATH,
        feedback_routes.FEEDBACK_API_PATH,
    }:
        return await http_exception_handler(request, exc)
    if exc.status_code == 401:
        body = chat_error(
            "UNAUTHORIZED",
            "Authorization failed.",
            request_id=await request_id_from_request(request),
            retryable=False,
            details=None,
        ).model_dump(mode="json")
        return JSONResponse(status_code=401, content=body, headers=exc.headers)
    return await http_exception_handler(request, exc)


@app.get("/health", include_in_schema=False)
async def healthcheck() -> dict[str, str]:
    return {"status": "ok", "runtime": "langgraph_native"}


@app.get("/health/ready", include_in_schema=False)
async def readinesscheck() -> JSONResponse:
    payload = await asyncio.to_thread(
        collect_agent_service_readiness,
        agent_engine=engine,
        backend_queries=mcp_tool_server.backend_queries,
        settings=get_settings(),
        provider=runtime_components.provider,
        verify_generation=False,
    )
    status_code = 200 if payload["status"] == "ready" else 503
    return JSONResponse(status_code=status_code, content=payload)


@app.get(
    "/health/generation/ready",
    dependencies=[Depends(require_agent_sync_bearer_token)],
    include_in_schema=False,
)
async def generation_readinesscheck() -> JSONResponse:
    payload = await asyncio.to_thread(
        collect_agent_service_readiness,
        agent_engine=engine,
        backend_queries=mcp_tool_server.backend_queries,
        settings=get_settings(),
        provider=runtime_components.provider,
        verify_generation=True,
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
    "async_clinician_alerts",
    "async_missed_dose_events",
    "async_push_messages",
    "async_task_action",
    "async_task_detail",
    "async_task_status",
    "async_tasks",
    "dead_async_tasks",
    "healthcheck",
    "generation_readinesscheck",
    "http_exception_contract_handler",
    "lifespan",
    "request_validation_error_handler",
    "readinesscheck",
    "sync_chat",
    "update_agent_model_config",
]
