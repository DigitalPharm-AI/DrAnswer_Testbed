from __future__ import annotations

import contextlib
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

import system_app.services.workers as worker_services
from system_app.db import SessionLocal, engine
from system_app.schema import verify_system_schema_current
from system_app.routes import (
    create_agent_api_router,
    create_agent_async_api_router,
    create_backend_v13_router,
    create_frontend_router,
    create_health_router,
    create_ui_api_router,
    create_ui_feedback_router,
)
from system_app.runtime import SystemRuntime
from system_app.openapi_v13 import install_system_v13_openapi
from system_app.services.agent_client import AgentClient
from system_app.services.audit_retention import (
    purge_expired_backend_audits,
)
from system_app.services.backend_v13_service import POLICY_CHANGE_PATH, RECORD_CHANGE_PATH
from system_app.services.policy_service import reload_policy_workbook
from system_app.routes.ui_api import UI_PREFIX, ui_error_body
from shared.backend_v13_contracts import (
    CommonErrorResponse,
    ContractError,
)
from shared.contract_errors import contract_error_definition
from shared.public_ids import request_id_from_body

agent_client = AgentClient()
_APP_DIR = Path(__file__).parent
_REACT_STATIC_DIR = _APP_DIR / "static" / "react"
write_lock = threading.RLock()

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
BACKEND_V13_CONTRACT_PATHS = frozenset({RECORD_CHANGE_PATH, POLICY_CHANGE_PATH})
ASYNC_CALLBACK_CONTRACT_PATHS = frozenset(
    {
        "/api/agent/async/missed-dose-results",
        "/api/agent/async/notification-policy-change-proposals",
    }
)


def sync_worker_dependencies() -> None:
    worker_services.SessionLocal = SessionLocal


def clock_worker(stop_event: threading.Event) -> None:
    sync_worker_dependencies()
    worker_services.clock_worker(stop_event, write_lock)


def notification_worker(stop_event: threading.Event) -> None:
    sync_worker_dependencies()
    worker_services.notification_worker(stop_event, write_lock)


def agent_worker(stop_event: threading.Event) -> None:
    sync_worker_dependencies()
    worker_services.agent_worker(stop_event, write_lock, agent_client)


def get_runtime() -> SystemRuntime:
    return SystemRuntime(
        write_lock=write_lock,
        agent_client=agent_client,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    from shared.settings import get_settings

    settings = get_settings()
    settings.require_internal_api_token()
    settings.require_backend_api_token()
    settings.require_system_postgresql()
    verify_system_schema_current(engine)
    audit_retention_result = purge_expired_backend_audits(
        SessionLocal
    )
    if any(audit_retention_result.values()):
        logging.getLogger("uvicorn.error").info(
            "backend_audit_retention_cleanup result=%s",
            audit_retention_result,
        )
    from system_app.services.nutrition_preference_service import seed_nutrition_ontology

    from system_app.services.food_search_service import seed_food_ref_from_csv
    from system_app.services.ui_medication_scenario_service import (
        ensure_initial_testbed_scenario_state,
        seed_test_medication_scenarios,
    )

    _csv_path = Path(__file__).parent.parent / "data" / "nutrition_db.csv"
    with SessionLocal() as session:
        seed_nutrition_ontology(session)
        seed_test_medication_scenarios(session)
        if _csv_path.exists():
            seed_food_ref_from_csv(session, _csv_path)
        session.commit()
    reload_policy_workbook()
    sync_worker_dependencies()
    worker_services.initialize_runtime_state(write_lock)
    with write_lock:
        with SessionLocal() as session:
            ensure_initial_testbed_scenario_state(session)
            session.commit()

    stop_event = threading.Event()
    clock_thread = threading.Thread(target=clock_worker, args=(stop_event,), name="simulation-clock-thread", daemon=True)
    alert_thread = threading.Thread(target=notification_worker, args=(stop_event,), name="simulation-alert-thread", daemon=True)
    agent_thread = threading.Thread(target=agent_worker, args=(stop_event,), name="simulation-agent-thread", daemon=True)
    clock_thread.start()
    alert_thread.start()
    agent_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        with contextlib.suppress(RuntimeError):
            clock_thread.join(timeout=2)
            alert_thread.join(timeout=2)
            agent_thread.join(timeout=2)


def create_app() -> FastAPI:
    fastapi_app = FastAPI(
        title="Medication Reminder System",
        version="1.3",
        lifespan=lifespan,
    )

    @fastapi_app.exception_handler(RequestValidationError)
    async def backend_v13_request_validation_handler(
        request: Request,
        exc: RequestValidationError,
    ):
        if request.url.path.startswith(UI_PREFIX):
            violations = [
                {
                    "location": [str(item) for item in error.get("loc", ())],
                    "type": str(error.get("type") or "validation_error"),
                    "message": str(error.get("msg") or "Invalid request."),
                }
                for error in exc.errors()
            ]
            return JSONResponse(
                status_code=422,
                content=ui_error_body(
                    "INVALID_REQUEST",
                    "Request schema or required field is invalid.",
                    details={"violations": violations},
                ),
            )
        if request.url.path in ASYNC_CALLBACK_CONTRACT_PATHS:
            violations = [
                {
                    "location": [
                        str(item)
                        for item in error.get("loc", ())
                    ],
                    "type": str(
                        error.get("type") or "validation_error",
                    ),
                    "message": str(
                        error.get("msg") or "Invalid request.",
                    ),
                }
                for error in exc.errors()
            ]
            business_error = _business_validation_error(
                request.url.path,
                exc,
            )
            code = (
                business_error
                if business_error is not None
                else "INVALID_REQUEST"
            )
            definition = contract_error_definition(code)
            body = CommonErrorResponse(
                request_id=request_id_from_body(exc.body),
                error=ContractError(
                    code=code,
                    message=definition.message,
                    retryable=False,
                    details={"violations": violations},
                ),
            )
            return JSONResponse(
                status_code=definition.status_code,
                content=body.model_dump(mode="json"),
            )
        if request.url.path not in BACKEND_V13_CONTRACT_PATHS:
            return await request_validation_exception_handler(request, exc)
        violations = [
            {
                "location": [str(item) for item in error.get("loc", ())],
                "type": str(error.get("type") or "validation_error"),
                "message": str(error.get("msg") or "Invalid request."),
            }
            for error in exc.errors()
        ]
        business_error = _business_validation_error(
            request.url.path,
            exc,
        )
        code = (
            business_error
            if business_error is not None
            else "INVALID_REQUEST"
        )
        definition = contract_error_definition(code)
        return _backend_v13_error_response(
            request_id=request_id_from_body(exc.body),
            status_code=definition.status_code,
            code=code,
            message=definition.message,
            details={"violations": violations},
        )

    @fastapi_app.exception_handler(StarletteHTTPException)
    async def backend_v13_http_exception_handler(
        request: Request,
        exc: StarletteHTTPException,
    ):
        if request.url.path.startswith(UI_PREFIX):
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            code = str(detail.get("code") or "HTTP_ERROR")
            message = str(
                detail.get("message")
                or (
                    exc.detail
                    if isinstance(exc.detail, str)
                    else code
                )
            )
            return JSONResponse(
                status_code=exc.status_code,
                content=ui_error_body(
                    code,
                    message,
                    retryable=bool(detail.get("retryable", False)),
                    details=(
                        detail.get("details")
                        if isinstance(detail.get("details"), dict)
                        else None
                    ),
                ),
            )
        if request.url.path in ASYNC_CALLBACK_CONTRACT_PATHS:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            body = CommonErrorResponse(
                request_id=await _request_id_from_request(request),
                error=ContractError(
                    code=str(
                        detail.get("code")
                        or (
                            "UNAUTHORIZED"
                            if exc.status_code == 401
                            else "BACKEND_PROCESSING_ERROR"
                        )
                    ),
                    message=str(
                        detail.get("message")
                        or (
                            "Authorization failed."
                            if exc.status_code == 401
                            else contract_error_definition(
                                "BACKEND_PROCESSING_ERROR"
                            ).message
                        )
                    ),
                    retryable=bool(
                        detail.get(
                            "retryable",
                            exc.status_code >= 500,
                        )
                    ),
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
        if request.url.path not in BACKEND_V13_CONTRACT_PATHS:
            return await http_exception_handler(request, exc)
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        code = str(detail.get("code") or ("UNAUTHORIZED" if exc.status_code == 401 else "INVALID_REQUEST"))
        return _backend_v13_error_response(
            request_id=await _request_id_from_request(request),
            status_code=exc.status_code,
            code=code,
            message=str(detail.get("message") or code),
            retryable=bool(detail.get("retryable", False)),
            details=detail.get("details") if isinstance(detail.get("details"), dict) else None,
        )

    @fastapi_app.middleware("http")
    async def prevent_stale_browser_cache(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            if not request.url.path.startswith(UI_PREFIX):
                raise
            response = JSONResponse(
                status_code=500,
                content=ui_error_body(
                    "BACKEND_PROCESSING_ERROR",
                    "The Backend could not complete the UI request.",
                    retryable=True,
                ),
            )
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    fastapi_app.mount(
        "/static/react",
        StaticFiles(directory=str(_REACT_STATIC_DIR)),
        name="react-static",
    )
    fastapi_app.include_router(create_frontend_router())
    fastapi_app.include_router(
        create_agent_api_router(get_runtime),
        include_in_schema=False,
    )
    fastapi_app.include_router(create_agent_async_api_router(get_runtime))
    fastapi_app.include_router(create_backend_v13_router(get_runtime))
    fastapi_app.include_router(create_ui_api_router(get_runtime))
    fastapi_app.include_router(create_ui_feedback_router(get_runtime))
    fastapi_app.include_router(
        create_health_router(),
        include_in_schema=False,
    )
    install_system_v13_openapi(fastapi_app)
    return fastapi_app


def _backend_v13_error_response(
    *,
    request_id: str | None,
    status_code: int,
    code: str,
    message: str,
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    body = CommonErrorResponse(
        request_id=request_id,
        error=ContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=details,
        ),
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
    )


_RECORD_BUSINESS_VALIDATION_MARKERS = (
    "create_record_id_and_expected_version_must_be_null",
    "create_payload_required",
    "record_id_required",
    "expected_version_required",
    "delete_payload_must_be_null",
    "mutation_payload_required",
    "parent_record_id_must_be_null",
    "parent_record_id_required",
    "operation_not_supported",
    "not_supported",
    "unsupported_",
    "only_supports",
    "payload_required",
    "payload_empty",
    "create_requires",
)
_POLICY_BUSINESS_VALIDATION_CODES = {
    "policy_effective_date_range_invalid": (
        "POLICY_EFFECTIVE_DATE_RANGE_INVALID"
    ),
    "policy_effective_date_range_too_large": (
        "POLICY_EFFECTIVE_DATE_RANGE_TOO_LARGE"
    ),
}


def _business_validation_error(
    path: str,
    exc: RequestValidationError,
) -> str | None:
    errors = exc.errors()
    text = " ".join(
        (
            f"{str(error.get('msg') or '')} "
            f"{str((error.get('ctx') or {}).get('error') or '')}"
        )
        for error in errors
    )
    if path == RECORD_CHANGE_PATH:
        if any(
            marker in text
            for marker in _RECORD_BUSINESS_VALIDATION_MARKERS
        ):
            return "BUSINESS_VALIDATION_FAILED"
        return None
    if path == POLICY_CHANGE_PATH:
        for marker, code in _POLICY_BUSINESS_VALIDATION_CODES.items():
            if marker in text:
                return code
        if any(
            marker in text
            for marker in (
                "policy_changes_empty",
                "policy_at_timing_requires_zero_offset",
                "apply_requires_changes",
                "keep_requires_null_changes",
            )
        ):
            return "BUSINESS_VALIDATION_FAILED"
        if any(
            error.get("type") in {"greater_than_equal", "less_than_equal"}
            and "changes" in error.get("loc", ())
            for error in errors
        ):
            return "POLICY_BOUNDARY_VIOLATION"
        return None
    if (
        path
        == "/api/agent/async/notification-policy-change-proposals"
    ):
        if any(
            marker in text
            for marker in (
                "policy_changes_empty",
                "policy_effective_date_range_invalid",
                "policy_effective_date_range_too_large",
                "policy_at_timing_requires_zero_offset",
            )
        ) or any(
            error.get("type") in {"greater_than_equal", "less_than_equal"}
            for error in errors
        ):
            return "INVALID_POLICY_PROPOSAL"
    return None


async def _request_id_from_request(request: Request) -> str | None:
    try:
        body = await request.json()
    except (ValueError, RuntimeError):
        return None
    return request_id_from_body(body)


app = create_app()

__all__ = [
    "agent_client",
    "agent_worker",
    "app",
    "clock_worker",
    "create_app",
    "get_runtime",
    "notification_worker",
    "write_lock",
]
