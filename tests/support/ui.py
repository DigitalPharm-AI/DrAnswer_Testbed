from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from system_app.db import get_session
from system_app.routes.ui_api import create_ui_api_router
from system_app.services import ui_status_service
from tests.helpers import build_system_engine


def build_ui_app(
    _workspace: Any = None,
    *,
    agent_client: Any = None,
    database_name: str = "ui_api",
):
    ui_status_service.reset_agent_readiness_cache()
    engine, _cleanup = build_system_engine(database_name)
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    runtime = SimpleNamespace(
        write_lock=threading.RLock(),
        agent_client=agent_client,
    )
    app = FastAPI()
    app.include_router(create_ui_api_router(lambda: runtime))

    def session_override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    return TestClient(app), sessions
