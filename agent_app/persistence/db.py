from __future__ import annotations

from shared.db import create_session_factory, session_dependency
from shared.settings import get_settings

engine, SessionLocal = create_session_factory(get_settings().agent_database_url)


def get_session():
    yield from session_dependency(SessionLocal)
