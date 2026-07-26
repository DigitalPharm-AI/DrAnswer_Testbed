from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine

import agent_app.main as agent_main
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import Base
from agent_app.readiness import collect_agent_service_readiness
from shared.settings import Settings


class HealthyBackendQueries:
    def verify_contract(self):
        return {
            "ok": True,
            "contract_version": "1.2",
            "dialect": "sqlite",
            "read_only": True,
            "views": [],
        }


class UnhealthyBackendQueries:
    def verify_contract(self):
        raise RuntimeError(
            "private database path token=must-not-leak patient=must-not-leak"
        )


def _agent_engine(tmp_path: Path, name: str = "agent-ready.db"):
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        future=True,
    )
    Base.metadata.create_all(engine)
    run_migrations(engine)
    return engine


def _settings(**updates) -> Settings:
    values = {
        "agent_sync_api_token": "sync-ready-token",
        "backend_api_token": "backend-ready-token",
        "system_base_url": "http://backend.test:8000",
        "agent_feedback_encryption_key": (
            "cHl0ZXN0LWZlZWRiYWNrLWVuY3J5cHRpb24ta2V5ISE="
        ),
        "agent_feedback_encryption_key_id": "pytest-feedback-v1",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def test_health_ready_returns_stable_200_contract(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _agent_engine(tmp_path)
    monkeypatch.setattr(agent_main, "engine", engine)
    monkeypatch.setattr(
        agent_main.mcp_tool_server,
        "backend_queries",
        HealthyBackendQueries(),
    )
    monkeypatch.setattr(agent_main, "get_settings", _settings)
    transport = httpx.ASGITransport(app=agent_main.app)

    async def request_ready():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get("/health/ready")

    try:
        response = asyncio.run(request_ready())
    finally:
        engine.dispose()

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "contract_version": "1.2",
        "components": {
            "agent_database": {"ok": True, "code": "OK"},
            "backend_read_database": {"ok": True, "code": "OK"},
            "agent_sync_auth": {"ok": True, "code": "OK"},
            "backend_write_api": {"ok": True, "code": "OK"},
            "feedback_encryption": {"ok": True, "code": "OK"},
        },
    }


def test_health_ready_returns_secret_free_503_component_codes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _agent_engine(tmp_path)
    duplicate_secret = "must-not-leak-shared-token"
    monkeypatch.setattr(agent_main, "engine", engine)
    monkeypatch.setattr(
        agent_main.mcp_tool_server,
        "backend_queries",
        UnhealthyBackendQueries(),
    )
    monkeypatch.setattr(
        agent_main,
        "get_settings",
        lambda: _settings(
            agent_sync_api_token=duplicate_secret,
            backend_api_token=duplicate_secret,
            system_base_url="not-a-url",
        ),
    )
    transport = httpx.ASGITransport(app=agent_main.app)

    async def request_ready():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get("/health/ready")

    try:
        response = asyncio.run(request_ready())
    finally:
        engine.dispose()

    assert response.status_code == 503
    payload = response.json()
    assert payload == {
        "status": "not_ready",
        "contract_version": "1.2",
        "components": {
            "agent_database": {"ok": True, "code": "OK"},
            "backend_read_database": {
                "ok": False,
                "code": "BACKEND_READ_CONTRACT_UNAVAILABLE",
            },
            "agent_sync_auth": {
                "ok": False,
                "code": "AGENT_SYNC_TOKEN_NOT_DEDICATED",
            },
            "backend_write_api": {
                "ok": False,
                "code": "BACKEND_API_TOKEN_NOT_DEDICATED",
            },
            "feedback_encryption": {"ok": True, "code": "OK"},
        },
    }
    assert duplicate_secret not in response.text
    assert "private database path" not in response.text
    assert "patient=must-not-leak" not in response.text


def test_readiness_detects_missing_agent_migrations_and_required_config(
    tmp_path: Path,
) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'unmigrated-agent.db').as_posix()}",
        future=True,
    )
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=None,
            settings=_settings(
                agent_sync_api_token="",
                backend_api_token="",
                system_base_url="",
                agent_feedback_encryption_key="",
            ),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"] == {
        "agent_database": {
            "ok": False,
            "code": "AGENT_MIGRATION_TABLE_MISSING",
        },
        "backend_read_database": {
            "ok": False,
            "code": "BACKEND_READ_NOT_CONFIGURED",
        },
        "agent_sync_auth": {
            "ok": False,
            "code": "AGENT_SYNC_TOKEN_MISSING",
        },
        "backend_write_api": {
            "ok": False,
            "code": "BACKEND_API_TOKEN_MISSING",
        },
        "feedback_encryption": {
            "ok": False,
            "code": "FEEDBACK_ENCRYPTION_KEY_MISSING",
        },
    }


def test_readiness_rejects_invalid_backend_base_url(tmp_path: Path) -> None:
    engine = _agent_engine(tmp_path, "invalid-url-agent.db")
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=HealthyBackendQueries(),
            settings=_settings(system_base_url="backend-without-scheme"),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"]["backend_write_api"] == {
        "ok": False,
        "code": "BACKEND_BASE_URL_INVALID",
    }


@pytest.mark.parametrize(
    ("settings_updates", "expected_code"),
    [
        (
            {
                "agent_feedback_encryption_key": "c2hvcnQ=",
            },
            "FEEDBACK_ENCRYPTION_KEY_INVALID",
        ),
        (
            {
                "agent_feedback_encryption_key_id": "",
            },
            "FEEDBACK_ENCRYPTION_KEY_ID_MISSING",
        ),
    ],
)
def test_readiness_rejects_invalid_feedback_encryption_without_leaking_secret(
    tmp_path: Path,
    settings_updates,
    expected_code: str,
) -> None:
    engine = _agent_engine(tmp_path, f"{expected_code}.db")
    secret = str(settings_updates.get("agent_feedback_encryption_key") or "")
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=HealthyBackendQueries(),
            settings=_settings(**settings_updates),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"]["feedback_encryption"] == {
        "ok": False,
        "code": expected_code,
    }
    assert not secret or secret not in str(payload)
