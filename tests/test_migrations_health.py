from __future__ import annotations

import asyncio
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from shared.backend_read_contract import BACKEND_READ_VIEW_DEFINITIONS
from shared.public_ids import is_public_id
from system_app.main import app
from system_app.migrations import (
    ensure_agent_job_request_ids,
    required_migration_versions,
)
from system_app.schema import (
    migrate_system_schema,
    verify_system_schema_current,
)
from system_app.services import health as health_service
from tests.helpers import build_system_engine

SYSTEM_POSTGRES_TESTS_CONFIGURED = bool(
    os.getenv("SYSTEM_POSTGRES_TEST_DATABASE_URL", "").strip()
)
requires_system_postgresql = pytest.mark.skipif(
    not SYSTEM_POSTGRES_TESTS_CONFIGURED,
    reason="SYSTEM_POSTGRES_TEST_DATABASE_URL is required",
)


class _NonPostgresqlEngine:
    class dialect:
        name = "sqlite"


def test_system_schema_migration_and_runtime_verifier_reject_non_postgresql():
    with pytest.raises(
        RuntimeError,
        match="system_database_postgresql_required",
    ):
        migrate_system_schema(_NonPostgresqlEngine())
    with pytest.raises(
        RuntimeError,
        match="system_database_postgresql_required",
    ):
        verify_system_schema_current(_NonPostgresqlEngine())


@requires_system_postgresql
def test_dedicated_system_migration_is_idempotent_and_publishes_read_contract():
    database_engine, cleanup = build_system_engine(
        "system_migration",
        create_models=False,
    )
    try:
        first = migrate_system_schema(database_engine)
        second = migrate_system_schema(database_engine)
        verified = verify_system_schema_current(database_engine)

        inspector = inspect(database_engine)
        assert first["schema_current"] is True
        assert set(first["applied_now"]) == set(
            required_migration_versions()
        )
        assert second["applied_now"] == []
        assert verified["current"] is True
        assert set(BACKEND_READ_VIEW_DEFINITIONS).issubset(
            set(inspector.get_view_names())
        )
        assert "chat_messages" in inspector.get_table_names()
        assert "ai_v13_chat_messages" in inspector.get_view_names()
        assert "simulation_patient_profiles" not in inspector.get_table_names()
        assert "agent_run_traces" not in inspector.get_table_names()
        assert "agent_run_steps" not in inspector.get_table_names()
    finally:
        cleanup()


@requires_system_postgresql
def test_agent_job_request_id_is_backfilled_for_legacy_schema():
    database_engine, cleanup = build_system_engine(
        "legacy_agent_job",
        create_models=False,
    )
    try:
        with database_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE agent_jobs ("
                    "id INTEGER PRIMARY KEY, "
                    "job_type VARCHAR(40) NOT NULL, "
                    "status VARCHAR(20) NOT NULL, "
                    "payload_json TEXT NOT NULL"
                    ")"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO agent_jobs "
                    "(id, job_type, status, payload_json) "
                    "VALUES (1, 'daily_pattern', 'pending', '{}')"
                )
            )

        ensure_agent_job_request_ids(database_engine)

        with database_engine.connect() as connection:
            request_id = connection.execute(
                text("SELECT request_id FROM agent_jobs WHERE id = 1")
            ).scalar_one()
        assert is_public_id(request_id, "request")
    finally:
        cleanup()


@requires_system_postgresql
def test_system_schema_rejects_retired_owned_data_tables():
    database_engine, cleanup = build_system_engine(
        "retired_schema_guard",
    )
    try:
        migrate_system_schema(database_engine)
        with database_engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE phr_patients "
                    "(id INTEGER PRIMARY KEY)"
                )
            )

        with pytest.raises(
            RuntimeError,
            match="system_database_retired_schema_present",
        ):
            verify_system_schema_current(database_engine)
    finally:
        cleanup()


@requires_system_postgresql
def test_health_details_returns_operational_shape():
    with TestClient(app) as client:
        response = client.get("/health/details")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert "warnings" in payload
    assert payload["database"]["ok"] is True
    assert "agent_server" in payload
    assert "agent_async" in payload
    assert "worker_available" in payload["agent_async"]
    assert payload["llm"]["owner"] == "agent_server"
    assert payload["llm"]["status"] == "delegated"
    assert payload["llm"]["readiness_endpoint"].endswith(
        "/health/generation/ready"
    )
    assert "required" in payload["internal_api"]
    assert "applied_versions" in payload["migrations"]
    assert payload["budgets"]["load"]["concurrency_target"] >= 1
    assert "token_prices_configured" in payload["budgets"]["cost"]


def test_agent_async_health_summarizes_worker_queue(monkeypatch):
    captured: dict[str, object] = {}

    class DummyAsyncClient:
        def __init__(self, *, timeout: float, **_: object) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers=None):
            captured.update(
                {
                    "url": url,
                    "headers": headers,
                    "timeout": self.timeout,
                }
            )
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "counts": {"pending": 2, "dead": 1, "done": 5},
                    "active_count": 2,
                    "workers": [
                        {
                            "worker_id": "worker-a",
                            "status": "running",
                            "last_error": (
                                "pytest private async worker error "
                                "peanut allergy token=secret-value"
                            ),
                        },
                        {"worker_id": "worker-b", "status": "stale"},
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(
        health_service.httpx,
        "AsyncClient",
        DummyAsyncClient,
    )

    payload = asyncio.run(
        health_service._agent_async_health(
            "http://agent.test/",
            "agent-token",
        )
    )

    assert captured["url"] == (
        "http://agent.test/agent/async/tasks/status"
    )
    assert captured["headers"] == {
        "X-Internal-Api-Token": "agent-token"
    }
    assert captured["timeout"] == 1.0
    assert payload["reachable"] is True
    assert payload["pending_count"] == 2
    assert payload["dead_count"] == 1
    assert payload["worker_count"] == 2
    assert payload["running_worker_count"] == 1
    assert payload["stale_worker_count"] == 1
    assert payload["worker_available"] is True
    assert payload["warnings"] == ["dead_tasks_present"]
    assert payload["workers"][0]["last_error"].startswith(
        "clinical text redacted"
    )
    assert "pytest private async worker error" not in str(payload)
    assert "secret-value" not in str(payload)
