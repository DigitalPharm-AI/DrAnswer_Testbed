from __future__ import annotations

import hmac
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from agent_app.ae_pro_ctcae import (
    ProCtcaeReferenceUnavailable,
    load_workbook,
)
from agent_app.persistence.migrations import required_migration_versions
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.backend_query import (
    BackendQueryTools,
    BackendReadContractError,
)
from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.settings import Settings


def collect_agent_service_readiness(
    *,
    agent_engine: Engine,
    backend_queries: BackendQueryTools | None,
    settings: Settings,
    provider: BaseLLMProvider | None = None,
    verify_generation: bool = False,
) -> dict[str, Any]:
    """Return a public, secret-free readiness contract for container probes."""

    components = {
        "agent_database": _agent_database_component(agent_engine),
        "backend_read_database": _backend_read_component(backend_queries),
        "agent_sync_auth": _agent_sync_auth_component(settings),
        "backend_write_api": _backend_write_api_component(settings),
        "feedback_encryption": _feedback_encryption_component(settings),
        "pro_ctcae_reference": _pro_ctcae_reference_component(settings),
        "generation_provider": _generation_provider_component(
            provider,
            verify_generation=verify_generation,
        ),
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
            dialect = connection.dialect.name
            if dialect != "postgresql":
                return _component(
                    False,
                    "AGENT_DATABASE_POSTGRESQL_REQUIRED",
                    dialect=dialect,
                    server_version="unsupported",
                    migration_version=None,
                )
            server_version = _database_server_version(connection)
            if "schema_migrations" not in inspect(connection).get_table_names():
                return _component(
                    False,
                    "AGENT_MIGRATION_TABLE_MISSING",
                    dialect=dialect,
                    server_version=server_version,
                    migration_version=None,
                )
            applied = set(
                str(version)
                for version in connection.execute(
                    text("SELECT version FROM schema_migrations")
                ).scalars()
            )
            migration_version = max(applied) if applied else None
        expected = set(required_migration_versions(engine))
        if not expected <= applied:
            return _component(
                False,
                "AGENT_MIGRATIONS_INCOMPLETE",
                dialect=dialect,
                server_version=server_version,
                migration_version=migration_version,
            )
    except SQLAlchemyTimeoutError:
        return _component(
            False,
            "AGENT_DATABASE_POOL_EXHAUSTED",
            dialect=engine.dialect.name,
            server_version="unknown",
            migration_version=None,
        )
    except Exception:
        return _component(
            False,
            "AGENT_DATABASE_UNAVAILABLE",
            dialect=engine.dialect.name,
            server_version="unknown",
            migration_version=None,
        )
    return _component(
        True,
        "OK",
        dialect=dialect,
        server_version=server_version,
        migration_version=migration_version,
    )


def _backend_read_component(
    backend_queries: BackendQueryTools | None,
) -> dict[str, Any]:
    if backend_queries is None:
        return _component(False, "BACKEND_READ_NOT_CONFIGURED")
    try:
        contract = backend_queries.verify_contract()
    except BackendReadContractError as exc:
        if str(exc) == "backend_read_database_pool_exhausted":
            return _component(
                False,
                "BACKEND_READ_DATABASE_POOL_EXHAUSTED",
            )
        return _component(False, "BACKEND_READ_CONTRACT_UNAVAILABLE")
    except Exception:
        return _component(False, "BACKEND_READ_CONTRACT_UNAVAILABLE")
    details = contract if isinstance(contract, dict) else {}
    return _component(
        True,
        "OK",
        dialect=str(details.get("dialect") or "unknown"),
        server_version=str(details.get("server_version") or "unknown"),
    )


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


def _pro_ctcae_reference_component(settings: Settings) -> dict[str, Any]:
    try:
        workbook = load_workbook(settings.pro_ctcae_workbook_path)
    except ProCtcaeReferenceUnavailable as exc:
        code = (
            "PRO_CTCAE_REFERENCE_MISSING"
            if str(exc) == "pro_ctcae_reference_workbook_missing"
            else "PRO_CTCAE_REFERENCE_INVALID"
        )
        return _component(False, code)
    except Exception:
        return _component(False, "PRO_CTCAE_REFERENCE_INVALID")
    return _component(
        True,
        "OK",
        symptom_count=len(workbook.parsed_entries),
        other_question_count=len(workbook.other_questions),
    )


def _generation_provider_component(
    provider: BaseLLMProvider | None,
    *,
    verify_generation: bool,
) -> dict[str, Any]:
    if provider is None:
        return _component(False, "GENERATION_PROVIDER_NOT_CONFIGURED")
    try:
        readiness = (
            provider.generation_readiness()
            if verify_generation
            else provider.configuration_readiness()
        )
    except Exception:
        return _component(False, "GENERATION_PROVIDER_CHECK_FAILED")
    return _component(readiness.ok, readiness.code)


def _component(
    ok: bool,
    code: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "code": code,
        **details,
    }


def _database_server_version(connection) -> str:
    if connection.dialect.name != "postgresql":
        raise RuntimeError(
            f"database_postgresql_required:{connection.dialect.name}"
        )
    version_info = connection.dialect.server_version_info or ()
    return ".".join(str(part) for part in version_info) or "unknown"
