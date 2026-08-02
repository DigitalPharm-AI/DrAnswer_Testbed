from __future__ import annotations

from pathlib import Path

import pytest

import shared.db as shared_db
from agent_app.persistence.schema import verify_agent_schema_current
from shared.db import DatabaseEngineConfig, create_database_engine
from shared.settings import Settings
from system_app.migrations import (
    MIGRATIONS as SYSTEM_MIGRATIONS,
)
from system_app.migrations import (
    UI_MEDICATION_PLAN_COLUMNS,
    VERSIONED_TABLE_COLUMNS,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_database_urls_are_postgresql_only(
    monkeypatch,
) -> None:
    monkeypatch.delenv("SYSTEM_DATABASE_URL", raising=False)
    monkeypatch.delenv("AGENT_DATABASE_URL", raising=False)
    settings = Settings(
        _env_file=None,
    )

    assert settings.system_database_url.startswith(
        "postgresql+psycopg://"
    )
    assert settings.agent_database_url.startswith(
        "postgresql+psycopg://"
    )
    assert settings.backend_read_database_url.startswith(
        "postgresql+psycopg://"
    )
    assert settings.system_startup_migrations_enabled is False
    assert settings.agent_startup_migrations_enabled is False


def test_postgresql_engine_uses_bounded_pool_and_session_timeouts(
    monkeypatch,
) -> None:
    sentinel = object()
    create_calls: list[tuple[str, dict]] = []
    session_calls: list[dict] = []

    def fake_create_engine(database_url: str, **kwargs):
        create_calls.append((database_url, kwargs))
        return sentinel

    def fake_configure(engine, **kwargs):
        assert engine is sentinel
        session_calls.append(kwargs)

    monkeypatch.setattr(shared_db, "create_engine", fake_create_engine)
    monkeypatch.setattr(
        shared_db,
        "configure_postgresql_session",
        fake_configure,
    )

    engine = create_database_engine(
        "postgresql+psycopg://user:secret@db/agent",
        config=DatabaseEngineConfig(
            pool_size=7,
            max_overflow=3,
            pool_timeout_seconds=4.5,
            pool_recycle_seconds=600,
            statement_timeout_ms=12_000,
            lock_timeout_ms=1_500,
        ),
    )

    assert engine is sentinel
    _database_url, kwargs = create_calls[0]
    assert kwargs == {
        "pool_pre_ping": True,
        "pool_size": 7,
        "max_overflow": 3,
        "pool_timeout": 4.5,
        "pool_recycle": 600,
        "future": True,
    }
    assert session_calls == [
        {
            "statement_timeout_ms": 12_000,
            "lock_timeout_ms": 1_500,
            "read_only": False,
        }
    ]


def test_zero_postgresql_timeouts_explicitly_disable_role_defaults(
    monkeypatch,
) -> None:
    callbacks = {}
    statements: list[str] = []
    committed: list[bool] = []

    def fake_listens_for(_engine, event_name):
        def decorate(callback):
            callbacks[event_name] = callback
            return callback

        return decorate

    class Cursor:
        def execute(self, statement: str) -> None:
            statements.append(statement)

        def close(self) -> None:
            return None

    class Connection:
        def cursor(self):
            return Cursor()

        def commit(self) -> None:
            committed.append(True)

    monkeypatch.setattr(
        shared_db.event,
        "listens_for",
        fake_listens_for,
    )
    shared_db.configure_postgresql_session(
        object(),
        statement_timeout_ms=0,
        lock_timeout_ms=0,
        read_only=False,
    )
    callbacks["connect"](Connection(), None)

    assert statements == [
        "SET SESSION statement_timeout = '0ms'",
        "SET SESSION lock_timeout = '0ms'",
    ]
    assert committed == [True]


def test_every_environment_requires_postgresql_and_dedicated_migrations() -> None:
    sqlite_settings = Settings(
        app_env="production",
        agent_database_url="sqlite:///agent.db",
        backend_read_database_url="sqlite:///backend.db",
        system_database_url="sqlite:///system.db",
        system_startup_migrations_enabled=False,
        agent_startup_migrations_enabled=False,
    )
    with pytest.raises(RuntimeError, match="AGENT_DATABASE_URL"):
        sqlite_settings.require_agent_postgresql()
    with pytest.raises(RuntimeError, match="BACKEND_READ_DATABASE_URL"):
        sqlite_settings.require_backend_read_postgresql()
    with pytest.raises(RuntimeError, match="SYSTEM_DATABASE_URL"):
        sqlite_settings.require_system_postgresql()

    postgres_settings = Settings(
        app_env="production",
        agent_database_url=(
            "postgresql+psycopg://agent@db/dranswer_agent?sslmode=require"
        ),
        backend_read_database_url=(
            "postgresql+psycopg://reader@db/dranswer_backend?sslmode=require"
        ),
        system_database_url=(
            "postgresql+psycopg://backend@db/dranswer_backend?sslmode=require"
        ),
        system_startup_migrations_enabled=True,
        agent_startup_migrations_enabled=True,
    )
    with pytest.raises(
        RuntimeError,
        match="AGENT_STARTUP_MIGRATIONS_ENABLED",
    ):
        postgres_settings.require_agent_postgresql()
    with pytest.raises(
        RuntimeError,
        match="SYSTEM_STARTUP_MIGRATIONS_ENABLED",
    ):
        postgres_settings.require_system_postgresql()


def test_production_postgresql_requires_tls_for_every_database_boundary() -> None:
    settings = Settings(
        app_env="production",
        agent_database_url="postgresql+psycopg://agent@db/agent",
        agent_migration_database_url="postgresql+psycopg://migrator@db/agent",
        backend_read_database_url="postgresql+psycopg://reader@db/backend",
        system_database_url="postgresql+psycopg://system@db/backend",
        system_migration_database_url="postgresql+psycopg://migrator@db/backend",
    )

    checks = (
        settings.require_agent_postgresql,
        settings.require_agent_migration_postgresql,
        settings.require_backend_read_postgresql,
        settings.require_system_postgresql,
        settings.require_system_migration_postgresql,
    )
    for check in checks:
        with pytest.raises(RuntimeError, match="sslmode"):
            check()


def test_runtime_factory_and_schema_verifier_reject_sqlite() -> None:
    with pytest.raises(ValueError, match="database_postgresql_required"):
        create_database_engine("sqlite+pysqlite:///:memory:")

    class NonPostgresqlEngine:
        class dialect:
            name = "sqlite"

    with pytest.raises(
        RuntimeError,
        match="agent_database_postgresql_required",
    ):
        verify_agent_schema_current(NonPostgresqlEngine())


def test_system_migration_ddl_contains_no_sqlite_type_or_boolean_defaults() -> None:
    statements = [
        sql.upper()
        for _version, sql in SYSTEM_MIGRATIONS
    ]
    statements.extend(
        column_type.upper()
        for column_type in UI_MEDICATION_PLAN_COLUMNS.values()
    )
    statements.extend(
        column_type.upper()
        for columns in VERSIONED_TABLE_COLUMNS.values()
        for column_type in columns.values()
    )
    migration_ddl = "\n".join(statements)

    assert "DATETIME" not in migration_ddl
    assert "BOOLEAN NOT NULL DEFAULT 0" not in migration_ddl
    assert "BOOLEAN NOT NULL DEFAULT 1" not in migration_ddl


def test_runtime_and_testbed_sources_contain_no_sqlite_connection_path() -> None:
    allowed_documentation = {
        (
            Path("shared/db.py"),
            "sqlite is intentionally excluded",
        ),
    }
    violations: list[str] = []
    for source_root in (
        "shared",
        "agent_app",
        "system_app",
        "contract_test_server",
        "tools",
    ):
        for source_path in (PROJECT_ROOT / source_root).rglob("*.py"):
            relative_path = source_path.relative_to(PROJECT_ROOT)
            for line_number, line in enumerate(
                source_path.read_text(encoding="utf-8").splitlines(),
                start=1,
            ):
                lowered = line.lower()
                if "sqlite" not in lowered:
                    continue
                if any(
                    relative_path == allowed_path
                    and allowed_fragment in lowered
                    for allowed_path, allowed_fragment in allowed_documentation
                ):
                    continue
                violations.append(f"{relative_path}:{line_number}")

    assert violations == []
