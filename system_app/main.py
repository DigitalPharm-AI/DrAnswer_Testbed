from __future__ import annotations

import contextlib
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import system_app.services.workers as worker_services
from system_app.db import SessionLocal, engine
from system_app.migrations import run_migrations
from system_app.routes import (
    create_agent_api_router,
    create_agent_async_api_router,
    create_backend_v12_router,
    create_chat_router,
    create_health_router,
    create_medications_router,
    create_notifications_router,
    create_nutrition_router,
    create_pages_router,
    create_simulation_router,
)
from system_app.runtime import SystemRuntime
from system_app.services.agent_client import AgentClient
from system_app.services.phr_client import PhrClient
from system_app.services.policy_service import reload_policy_workbook

agent_client = AgentClient()
phr_client = PhrClient()
_APP_DIR = Path(__file__).parent
_STATIC_DIR = _APP_DIR / "static"
templates = Jinja2Templates(directory=str(_APP_DIR / "templates"))
write_lock = threading.RLock()

logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def static_version(path: str) -> str:
    static_path = _STATIC_DIR / path.lstrip("/\\")
    try:
        return str(int(static_path.stat().st_mtime))
    except OSError:
        return "0"


templates.env.globals["static_version"] = static_version


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


def system_event_worker(event_type: str, message: str, notification_id: int) -> None:
    sync_worker_dependencies()
    worker_services.system_event_worker(event_type, message, notification_id, write_lock, agent_client)


def mutation_confirmation_worker(confirmation_id: str, resolution: str) -> None:
    sync_worker_dependencies()
    worker_services.mutation_confirmation_worker(
        confirmation_id,
        resolution,
        write_lock,
        agent_client,
    )


def get_runtime() -> SystemRuntime:
    return SystemRuntime(
        templates=templates,
        write_lock=write_lock,
        agent_client=agent_client,
        phr_client=phr_client,
        system_event_worker=system_event_worker,
        mutation_confirmation_worker=mutation_confirmation_worker,
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    from shared.settings import get_settings

    get_settings().require_internal_api_token_in_production()
    get_settings().require_backend_api_token_in_production()
    run_migrations(engine)
    from system_app.services.nutrition_preference_service import seed_nutrition_ontology

    from system_app.services.food_search_service import seed_food_ref_from_csv

    _csv_path = Path(__file__).parent.parent / "data" / "nutrition_db.csv"
    with SessionLocal() as session:
        seed_nutrition_ontology(session)
        if _csv_path.exists():
            seed_food_ref_from_csv(session, _csv_path)
        session.commit()
    reload_policy_workbook()
    sync_worker_dependencies()
    worker_services.initialize_runtime_state(write_lock)

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
    fastapi_app = FastAPI(title="Medication Reminder System", lifespan=lifespan)

    @fastapi_app.middleware("http")
    async def prevent_stale_browser_cache(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    fastapi_app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
    fastapi_app.include_router(create_pages_router(get_runtime))
    fastapi_app.include_router(create_medications_router(get_runtime))
    fastapi_app.include_router(create_simulation_router(get_runtime))
    fastapi_app.include_router(create_notifications_router(get_runtime))
    fastapi_app.include_router(create_nutrition_router(get_runtime))
    fastapi_app.include_router(create_chat_router(get_runtime))
    fastapi_app.include_router(create_agent_api_router(get_runtime))
    fastapi_app.include_router(create_agent_async_api_router(get_runtime))
    fastapi_app.include_router(create_backend_v12_router(get_runtime))
    fastapi_app.include_router(create_health_router())
    return fastapi_app


app = create_app()

__all__ = [
    "agent_client",
    "agent_worker",
    "app",
    "clock_worker",
    "create_app",
    "get_runtime",
    "notification_worker",
    "mutation_confirmation_worker",
    "phr_client",
    "system_event_worker",
    "templates",
    "write_lock",
]
