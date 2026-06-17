from __future__ import annotations

from shared.db import create_sqlite_session_factory, session_dependency
from shared.settings import get_settings

settings = get_settings()
engine, SessionLocal = create_sqlite_session_factory(settings.system_database_url)


def get_session():
    yield from session_dependency(SessionLocal)
