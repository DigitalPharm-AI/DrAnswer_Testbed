from __future__ import annotations

import threading
from datetime import timedelta
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from system_app.db import get_session
from system_app.models import BackendApiRequest
from system_app.routes.ui_api import create_ui_api_router
from system_app.services.clock_service import ensure_clock
from system_app.services.ui_medication_scenario_service import (
    ensure_initial_testbed_scenario_state,
    seed_test_medication_scenarios,
)
from tests.helpers import build_system_engine


def test_ui_clock_advance_replays_without_advancing_twice(tmp_path) -> None:
    engine, _cleanup = build_system_engine(
        "ui_clock_idempotency"
    )
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    runtime = SimpleNamespace(
        write_lock=threading.RLock(),
        agent_client=None,
    )
    app = FastAPI()
    app.include_router(create_ui_api_router(lambda: runtime))

    def session_override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    client = TestClient(app)

    with sessions() as session:
        seed_test_medication_scenarios(session)
        ensure_initial_testbed_scenario_state(session)
        initial_time = ensure_clock(session).current_time
        session.commit()

    payload = {
        "request_id": "req_0000000000000001",
        "minutes": 30,
    }
    first = client.post("/api/ui/v1/clock/advance", json=payload)
    replay = client.post("/api/ui/v1/clock/advance", json=payload)
    conflict = client.post(
        "/api/ui/v1/clock/advance",
        json={**payload, "minutes": 180},
    )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    with sessions() as session:
        assert ensure_clock(session).current_time == initial_time + timedelta(
            minutes=30
        )
        assert (
            session.query(BackendApiRequest)
            .filter_by(api_path="/api/ui/v1/clock/advance")
            .count()
            == 1
        )
