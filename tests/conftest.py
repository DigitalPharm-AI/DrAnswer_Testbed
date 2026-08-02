from __future__ import annotations

import atexit
import os
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

TEST_DATA_DIR = Path("data") / "pytest_runtime"
TEST_DATA_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_POSTGRES_TEST_DATABASE_URL = os.getenv(
    "SYSTEM_POSTGRES_TEST_DATABASE_URL",
    "",
).strip()
AGENT_POSTGRES_TEST_DATABASE_URL = os.getenv(
    "AGENT_POSTGRES_TEST_DATABASE_URL",
    "",
).strip()
POSTGRES_TEST_DATABASES_CONFIGURED = bool(
    SYSTEM_POSTGRES_TEST_DATABASE_URL
    and AGENT_POSTGRES_TEST_DATABASE_URL
)


def _isolated_session_database_url(
    database_url: str,
    *,
    prefix: str,
) -> str:
    base_url = make_url(database_url)
    if not base_url.drivername.startswith("postgresql"):
        raise RuntimeError(f"{prefix} test database must use PostgreSQL")

    schema_name = f"{prefix}_{uuid4().hex}"
    admin_engine = create_engine(
        base_url,
        future=True,
        poolclass=NullPool,
    )
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema_name}"
    isolated_url = base_url.set(query=query)
    probe_engine = create_engine(
        isolated_url,
        future=True,
        poolclass=NullPool,
    )
    try:
        with probe_engine.connect() as connection:
            current_schema = connection.execute(
                text("SELECT current_schema()")
            ).scalar_one()
        if current_schema != schema_name:
            raise RuntimeError(
                "postgresql_test_schema_isolation_failed:"
                f"{current_schema}"
            )
    except Exception:
        with admin_engine.begin() as connection:
            connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            )
        admin_engine.dispose()
        raise
    finally:
        probe_engine.dispose()

    def cleanup() -> None:
        try:
            with admin_engine.begin() as connection:
                connection.execute(
                    text(
                        f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
                    )
                )
        finally:
            admin_engine.dispose()

    atexit.register(cleanup)
    return isolated_url.render_as_string(hide_password=False)


if POSTGRES_TEST_DATABASES_CONFIGURED:
    system_runtime_database_url = _isolated_session_database_url(
        SYSTEM_POSTGRES_TEST_DATABASE_URL,
        prefix="pytest_system",
    )
    agent_runtime_database_url = _isolated_session_database_url(
        AGENT_POSTGRES_TEST_DATABASE_URL,
        prefix="pytest_agent",
    )
else:
    system_runtime_database_url = (
        "postgresql+psycopg://pytest_unconfigured@127.0.0.1:5432/"
        "dranswer_system_pytest"
    )
    agent_runtime_database_url = (
        "postgresql+psycopg://pytest_unconfigured@127.0.0.1:5432/"
        "dranswer_agent_pytest"
    )

os.environ["SYSTEM_DATABASE_URL"] = system_runtime_database_url
os.environ["AGENT_DATABASE_URL"] = agent_runtime_database_url
os.environ["SYSTEM_STARTUP_MIGRATIONS_ENABLED"] = "false"
os.environ["AGENT_STARTUP_MIGRATIONS_ENABLED"] = "false"
os.environ["PROMPT_WORKBOOK_PATH"] = str(TEST_DATA_DIR / "prompt_registry.xlsx")
os.environ["POLICY_WORKBOOK_PATH"] = str(TEST_DATA_DIR / "default_notification_policies.xlsx")
os.environ["AGENT_SYNC_API_TOKEN"] = "pytest-agent-sync-token"
os.environ["INTERNAL_API_TOKEN"] = "pytest-internal-api-token"
os.environ["AGENT_FEEDBACK_ENCRYPTION_KEY"] = (
    "cHl0ZXN0LWZlZWRiYWNrLWVuY3J5cHRpb24ta2V5ISE="
)
os.environ["AGENT_FEEDBACK_ENCRYPTION_KEY_ID"] = "pytest-feedback-v1"

from agent_app.persistence.db import engine as agent_engine  # noqa: E402
from agent_app.persistence.schema import migrate_agent_schema  # noqa: E402
from system_app.db import engine as system_engine  # noqa: E402
from system_app.schema import migrate_system_schema  # noqa: E402

if POSTGRES_TEST_DATABASES_CONFIGURED:
    migrate_system_schema(system_engine)
    migrate_agent_schema(agent_engine)
