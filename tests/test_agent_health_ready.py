from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

import agent_app.main as agent_main
from agent_app.persistence.migrations import (
    required_migration_versions,
    run_migrations,
)
from agent_app.providers.base import GenerationReadiness
from agent_app.readiness import (
    _agent_database_component,
    _backend_read_component,
    collect_agent_service_readiness,
)
from agent_app.tools.backend_query import BackendReadContractError
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.settings import Settings
from shared.settings import get_settings as get_shared_settings
from tests.helpers import build_agent_engine


class HealthyBackendQueries:
    def verify_contract(self):
        return {
            "ok": True,
            "contract_version": BACKEND_READ_CONTRACT_VERSION,
            "dialect": "postgresql",
            "server_version": "test-version",
            "read_only": True,
            "views": [],
        }


class UnhealthyBackendQueries:
    def verify_contract(self):
        raise RuntimeError(
            "private database path token=must-not-leak patient=must-not-leak"
        )


class HealthyGenerationProvider:
    def configuration_readiness(self) -> GenerationReadiness:
        return GenerationReadiness(
            ok=True,
            code="GENERATION_PROVIDER_CONFIGURED",
        )

    def generation_readiness(self) -> GenerationReadiness:
        return GenerationReadiness(ok=True, code="OK")


def _agent_engine(_tmp_path=None, _name: str = "agent_ready"):
    engine, _cleanup = build_agent_engine("agent_health_ready")
    run_migrations(engine)
    return engine


def _settings(**updates) -> Settings:
    values = {
        "agent_sync_api_token": "sync-ready-token",
        "internal_api_token": "internal-ready-token",
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
    monkeypatch.setattr(
        agent_main.runtime_components.provider,
        "generation_readiness",
        HealthyGenerationProvider().generation_readiness,
    )
    monkeypatch.setattr(
        agent_main.runtime_components.provider,
        "configuration_readiness",
        HealthyGenerationProvider().configuration_readiness,
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
        "contract_version": BACKEND_READ_CONTRACT_VERSION,
        "components": {
            "agent_database": {
                "ok": True,
                "code": "OK",
                "dialect": "postgresql",
                "server_version": response.json()["components"][
                    "agent_database"
                ]["server_version"],
                "migration_version": (
                    required_migration_versions(engine)[-1]
                ),
            },
            "backend_read_database": {
                "ok": True,
                "code": "OK",
                "dialect": "postgresql",
                "server_version": "test-version",
            },
            "agent_sync_auth": {"ok": True, "code": "OK"},
            "backend_write_api": {"ok": True, "code": "OK"},
            "feedback_encryption": {"ok": True, "code": "OK"},
            "pro_ctcae_reference": {
                "ok": True,
                "code": "OK",
                "symptom_count": response.json()["components"][
                    "pro_ctcae_reference"
                ]["symptom_count"],
                "other_question_count": response.json()["components"][
                    "pro_ctcae_reference"
                ]["other_question_count"],
            },
            "generation_provider": {
                "ok": True,
                "code": "GENERATION_PROVIDER_CONFIGURED",
            },
        },
    }


def test_generation_readiness_requires_auth_and_uses_real_provider_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    engine = _agent_engine(tmp_path, "agent_generation_ready")
    monkeypatch.setattr(agent_main, "engine", engine)
    monkeypatch.setattr(
        agent_main.mcp_tool_server,
        "backend_queries",
        HealthyBackendQueries(),
    )
    monkeypatch.setattr(
        agent_main.runtime_components.provider,
        "generation_readiness",
        HealthyGenerationProvider().generation_readiness,
    )
    monkeypatch.setattr(agent_main, "get_settings", _settings)
    transport = httpx.ASGITransport(app=agent_main.app)

    async def request_ready():
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            unauthorized = await client.get(
                "/health/generation/ready"
            )
            authorized = await client.get(
                "/health/generation/ready",
                headers={
                    "Authorization": (
                        "Bearer "
                        + get_shared_settings().require_service_api_token()
                    )
                },
            )
            return unauthorized, authorized

    try:
        unauthorized, authorized = asyncio.run(request_ready())
    finally:
        engine.dispose()

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
    payload = authorized.json()
    assert payload["status"] == "ready"
    assert payload["components"]["generation_provider"] == {
        "ok": True,
        "code": "OK",
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
        agent_main.runtime_components.provider,
        "generation_readiness",
        HealthyGenerationProvider().generation_readiness,
    )
    monkeypatch.setattr(
        agent_main.runtime_components.provider,
        "configuration_readiness",
        HealthyGenerationProvider().configuration_readiness,
    )
    monkeypatch.setattr(
        agent_main,
        "get_settings",
        lambda: _settings(
            agent_sync_api_token=duplicate_secret,
            internal_api_token=duplicate_secret,
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
        "contract_version": BACKEND_READ_CONTRACT_VERSION,
        "components": {
            "agent_database": {
                "ok": True,
                "code": "OK",
                "dialect": "postgresql",
                "server_version": payload["components"][
                    "agent_database"
                ]["server_version"],
                "migration_version": (
                    required_migration_versions(engine)[-1]
                ),
            },
            "backend_read_database": {
                "ok": False,
                "code": "BACKEND_READ_CONTRACT_UNAVAILABLE",
            },
            "agent_sync_auth": {
                "ok": False,
                "code": "SERVICE_API_TOKEN_INVALID",
            },
            "backend_write_api": {
                "ok": False,
                "code": "SERVICE_API_TOKEN_INVALID",
            },
            "feedback_encryption": {"ok": True, "code": "OK"},
            "pro_ctcae_reference": {
                "ok": True,
                "code": "OK",
                "symptom_count": payload["components"][
                    "pro_ctcae_reference"
                ]["symptom_count"],
                "other_question_count": payload["components"][
                    "pro_ctcae_reference"
                ]["other_question_count"],
            },
            "generation_provider": {
                "ok": True,
                "code": "GENERATION_PROVIDER_CONFIGURED",
            },
        },
    }
    assert duplicate_secret not in response.text
    assert "private database path" not in response.text
    assert "patient=must-not-leak" not in response.text


def test_readiness_detects_missing_agent_migrations_and_required_config(
    tmp_path: Path,
) -> None:
    engine, cleanup = build_agent_engine(
        "agent_health_unmigrated",
        create_models=False,
    )
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=None,
            settings=_settings(
                agent_sync_api_token="",
                system_base_url="",
                agent_feedback_encryption_key="",
            ),
            provider=None,
        )
    finally:
        cleanup()

    assert payload["status"] == "not_ready"
    assert payload["components"] == {
            "agent_database": {
                "ok": False,
                "code": "AGENT_MIGRATION_TABLE_MISSING",
                "dialect": "postgresql",
                "server_version": payload["components"][
                    "agent_database"
                ]["server_version"],
                "migration_version": None,
            },
        "backend_read_database": {
            "ok": False,
            "code": "BACKEND_READ_NOT_CONFIGURED",
        },
        "agent_sync_auth": {
            "ok": False,
            "code": "SERVICE_API_TOKEN_INVALID",
        },
        "backend_write_api": {
            "ok": False,
            "code": "SERVICE_API_TOKEN_INVALID",
        },
        "feedback_encryption": {
            "ok": False,
            "code": "FEEDBACK_ENCRYPTION_KEY_MISSING",
        },
        "pro_ctcae_reference": {
            "ok": True,
            "code": "OK",
            "symptom_count": payload["components"][
                "pro_ctcae_reference"
            ]["symptom_count"],
            "other_question_count": payload["components"][
                "pro_ctcae_reference"
            ]["other_question_count"],
        },
        "generation_provider": {
            "ok": False,
            "code": "GENERATION_PROVIDER_NOT_CONFIGURED",
        },
    }


def test_readiness_rejects_invalid_backend_base_url(tmp_path: Path) -> None:
    engine = _agent_engine(tmp_path, "invalid_url_agent")
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=HealthyBackendQueries(),
            settings=_settings(system_base_url="backend-without-scheme"),
            provider=HealthyGenerationProvider(),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"]["backend_write_api"] == {
        "ok": False,
        "code": "BACKEND_BASE_URL_INVALID",
    }


def test_readiness_rejects_missing_pro_ctcae_reference(
    tmp_path: Path,
) -> None:
    engine = _agent_engine(tmp_path, "missing_pro_ctcae")
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=HealthyBackendQueries(),
            settings=_settings(
                pro_ctcae_workbook_path=tmp_path / "missing.xlsx",
            ),
            provider=HealthyGenerationProvider(),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"]["pro_ctcae_reference"] == {
        "ok": False,
        "code": "PRO_CTCAE_REFERENCE_MISSING",
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
    engine = _agent_engine(tmp_path, expected_code)
    secret = str(settings_updates.get("agent_feedback_encryption_key") or "")
    try:
        payload = collect_agent_service_readiness(
            agent_engine=engine,
            backend_queries=HealthyBackendQueries(),
            settings=_settings(**settings_updates),
            provider=HealthyGenerationProvider(),
        )
    finally:
        engine.dispose()

    assert payload["status"] == "not_ready"
    assert payload["components"]["feedback_encryption"] == {
        "ok": False,
        "code": expected_code,
    }
    assert not secret or secret not in str(payload)


def test_readiness_distinguishes_database_pool_exhaustion() -> None:
    class ExhaustedEngine:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def connect(self):
            raise SQLAlchemyTimeoutError("pool timeout")

    class ExhaustedBackendQueries:
        def verify_contract(self):
            raise BackendReadContractError(
                "backend_read_database_pool_exhausted"
            )

    assert _agent_database_component(ExhaustedEngine()) == {
        "ok": False,
        "code": "AGENT_DATABASE_POOL_EXHAUSTED",
        "dialect": "postgresql",
        "server_version": "unknown",
        "migration_version": None,
    }
    assert _backend_read_component(ExhaustedBackendQueries()) == {
        "ok": False,
        "code": "BACKEND_READ_DATABASE_POOL_EXHAUSTED",
    }
