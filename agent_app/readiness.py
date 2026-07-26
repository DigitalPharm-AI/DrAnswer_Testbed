from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Engine, inspect, text

from agent_app.persistence.migrations import required_migration_versions
from agent_app.tools.backend_query import BackendQueryTools
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.settings import Settings


def collect_agent_service_readiness(
    *,
    agent_engine: Engine,
    backend_queries: BackendQueryTools | None,
    settings: Settings,
) -> dict[str, Any]:
    """Return a public, secret-free readiness contract for container probes."""

    components = {
        "agent_database": _agent_database_component(agent_engine),
        "backend_read_database": _backend_read_component(backend_queries),
        "agent_sync_auth": _agent_sync_auth_component(settings),
        "backend_write_api": _backend_write_api_component(settings),
        "feedback_encryption": _feedback_encryption_component(settings),
    }
    ready = all(bool(component["ok"]) for component in components.values())
    return {
        "status": "ready" if ready else "not_ready",
        "contract_version": BACKEND_READ_CONTRACT_VERSION,
        "components": components,
    }


def _agent_database_component(engine: Engine) -> dict[str, Any]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1")).scalar_one()
            if "schema_migrations" not in inspect(connection).get_table_names():
                return _component(False, "AGENT_MIGRATION_TABLE_MISSING")
            applied = set(
                str(version)
                for version in connection.execute(
                    text("SELECT version FROM schema_migrations")
                ).scalars()
            )
        expected = set(required_migration_versions(engine))
        if not expected <= applied:
            return _component(False, "AGENT_MIGRATIONS_INCOMPLETE")
    except Exception:
        return _component(False, "AGENT_DATABASE_UNAVAILABLE")
    return _component(True, "OK")


def _backend_read_component(
    backend_queries: BackendQueryTools | None,
) -> dict[str, Any]:
    if backend_queries is None:
        return _component(False, "BACKEND_READ_NOT_CONFIGURED")
    try:
        backend_queries.verify_contract()
    except Exception:
        return _component(False, "BACKEND_READ_CONTRACT_UNAVAILABLE")
    return _component(True, "OK")


def _agent_sync_auth_component(settings: Settings) -> dict[str, Any]:
    agent_token = (settings.agent_sync_api_token or "").strip()
    backend_token = (settings.backend_api_token or "").strip()
    if not agent_token:
        return _component(False, "AGENT_SYNC_TOKEN_MISSING")
    if backend_token and hmac.compare_digest(agent_token, backend_token):
        return _component(False, "AGENT_SYNC_TOKEN_NOT_DEDICATED")
    return _component(True, "OK")


def _backend_write_api_component(settings: Settings) -> dict[str, Any]:
    backend_token = (settings.backend_api_token or "").strip()
    agent_token = (settings.agent_sync_api_token or "").strip()
    if not backend_token:
        return _component(False, "BACKEND_API_TOKEN_MISSING")
    if agent_token and hmac.compare_digest(backend_token, agent_token):
        return _component(False, "BACKEND_API_TOKEN_NOT_DEDICATED")
    parsed = urlsplit(settings.system_base_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return _component(False, "BACKEND_BASE_URL_INVALID")
    return _component(True, "OK")


def _feedback_encryption_component(
    settings: Settings,
) -> dict[str, Any]:
    secret = settings.agent_feedback_encryption_key
    encoded = (
        secret.get_secret_value().strip()
        if secret is not None
        else ""
    )
    if not encoded:
        return _component(False, "FEEDBACK_ENCRYPTION_KEY_MISSING")
    if not settings.agent_feedback_encryption_key_id.strip():
        return _component(False, "FEEDBACK_ENCRYPTION_KEY_ID_MISSING")
    try:
        settings.require_agent_feedback_encryption()
    except RuntimeError:
        return _component(False, "FEEDBACK_ENCRYPTION_KEY_INVALID")
    return _component(True, "OK")


def _component(ok: bool, code: str) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
    }
