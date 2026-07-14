from __future__ import annotations

from collections.abc import AsyncIterator

import anyio
from sqlalchemy.orm import Session

from shared.db import create_sqlite_session_factory
from shared.settings import get_settings

settings = get_settings()
engine, SessionLocal = create_sqlite_session_factory(settings.system_database_url)


async def get_session() -> AsyncIterator[Session]:
    session = SessionLocal()
    try:
        # Pool checkout may wait. Keep that wait off the event loop, then close
        # here so connection checkin never depends on a saturated thread pool.
        await anyio.to_thread.run_sync(session.connection)
        yield session
    finally:
        session.close()
