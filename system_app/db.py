from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
from sqlalchemy.orm import Session

from shared.db import DatabaseEngineConfig, create_session_factory
from shared.settings import get_settings

settings = get_settings()
engine, SessionLocal = create_session_factory(
    settings.system_database_url,
    config=DatabaseEngineConfig(
        pool_size=settings.system_db_pool_size,
        max_overflow=settings.system_db_max_overflow,
        pool_timeout_seconds=settings.system_db_pool_timeout_seconds,
        pool_recycle_seconds=settings.system_db_pool_recycle_seconds,
        statement_timeout_ms=settings.system_db_statement_timeout_ms,
        lock_timeout_ms=settings.system_db_lock_timeout_ms,
    ),
)


async def get_session() -> AsyncIterator[Session]:
    session = SessionLocal()
    try:
        # Pool checkout may wait. Keep that wait off the event loop, then close
        # here so connection checkin never depends on a saturated thread pool.
        await anyio.to_thread.run_sync(session.connection)
        yield session
    finally:
        session.close()
