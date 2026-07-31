from __future__ import annotations

from shared.db import (
    DatabaseEngineConfig,
    create_session_factory,
    session_dependency,
)
from shared.settings import get_settings

settings = get_settings()
engine, SessionLocal = create_session_factory(
    settings.agent_database_url,
    config=DatabaseEngineConfig(
        pool_size=settings.agent_db_pool_size,
        max_overflow=settings.agent_db_max_overflow,
        pool_timeout_seconds=settings.agent_db_pool_timeout_seconds,
        pool_recycle_seconds=settings.agent_db_pool_recycle_seconds,
        statement_timeout_ms=settings.agent_db_statement_timeout_ms,
        lock_timeout_ms=settings.agent_db_lock_timeout_ms,
    ),
)


def get_session():
    yield from session_dependency(SessionLocal)
