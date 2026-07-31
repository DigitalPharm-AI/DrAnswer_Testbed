from __future__ import annotations

import atexit
import os
import uuid
from collections.abc import Callable

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from agent_app.persistence.models import Base as AgentBase
from shared.backend_read_contract import BACKEND_READ_VIEW_COLUMNS
from system_app.models import Base


def _isolated_postgresql_engine(
    prefix: str,
    *,
    database_env: str = "SYSTEM_POSTGRES_TEST_DATABASE_URL",
    metadata=Base.metadata,
    create_models: bool = True,
) -> tuple[Engine, Callable[[], None]]:
    raw_url = os.getenv(
        database_env,
        "",
    ).strip()
    if not raw_url:
        raise RuntimeError(
            f"{database_env} is required for DB tests"
        )
    base_url = make_url(raw_url)
    if not base_url.drivername.startswith("postgresql"):
        raise RuntimeError(
            f"{database_env} must use PostgreSQL"
        )

    schema_name = f"{prefix[:24]}_{uuid.uuid4().hex}"
    admin_engine = create_engine(
        base_url,
        future=True,
        poolclass=NullPool,
    )
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    query = dict(base_url.query)
    # Do not include ``public`` in the test search path. SQLAlchemy's
    # check-first lookup would otherwise see an identically named public
    # table and skip creating the isolated copy.
    query["options"] = f"-csearch_path={schema_name}"
    engine = create_engine(
        base_url.set(query=query),
        future=True,
        poolclass=NullPool,
    )
    with engine.connect() as connection:
        current_schema = connection.execute(
            text("SELECT current_schema()")
        ).scalar_one()
    if current_schema != schema_name:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            )
        admin_engine.dispose()
        raise RuntimeError(
            "postgresql_test_schema_isolation_failed:"
            f"{current_schema}"
        )
    if create_models:
        metadata.create_all(bind=engine)
    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        engine.dispose()
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
    return engine, cleanup


def build_system_engine(
    prefix: str = "system_test",
    *,
    create_models: bool = True,
) -> tuple[Engine, Callable[[], None]]:
    return _isolated_postgresql_engine(
        prefix,
        database_env="SYSTEM_POSTGRES_TEST_DATABASE_URL",
        metadata=Base.metadata,
        create_models=create_models,
    )


def build_agent_engine(
    prefix: str = "agent_test",
    *,
    create_models: bool = True,
) -> tuple[Engine, Callable[[], None]]:
    return _isolated_postgresql_engine(
        prefix,
        database_env="AGENT_POSTGRES_TEST_DATABASE_URL",
        metadata=AgentBase.metadata,
        create_models=create_models,
    )


def build_backend_reader_url(
    owner_engine: Engine,
    prefix: str = "backend_reader",
) -> tuple[str, Callable[[], None]]:
    """Provision a least-privilege reader for one isolated System schema."""

    role_name = f"{prefix[:24]}_{uuid.uuid4().hex}"
    password = uuid.uuid4().hex + uuid.uuid4().hex
    owner_url = make_url(
        owner_engine.url.render_as_string(hide_password=False)
    )
    admin_url = owner_url.set(
        query={
            key: value
            for key, value in owner_url.query.items()
            if key != "options"
        }
    )
    admin_engine = create_engine(
        admin_url,
        future=True,
        poolclass=NullPool,
    )
    with owner_engine.connect() as connection:
        schema_name = str(
            connection.execute(text("SELECT current_schema()")).scalar_one()
        )
        database_name = str(
            connection.execute(text("SELECT current_database()")).scalar_one()
        )
    if not schema_name or schema_name == "public":
        admin_engine.dispose()
        raise RuntimeError(
            "backend_reader_requires_isolated_test_schema"
        )

    qualified_views = ", ".join(
        f'"{schema_name}"."{view_name}"'
        for view_name in BACKEND_READ_VIEW_COLUMNS
    )
    configured_reader = os.getenv(
        "BACKEND_READ_DATABASE_URL",
        "",
    ).strip()
    if configured_reader:
        reader_base_url = make_url(configured_reader)
        if (
            reader_base_url.drivername.startswith("postgresql")
            and reader_base_url.database == database_name
            and reader_base_url.username
        ):
            quoted_reader = owner_engine.dialect.identifier_preparer.quote(
                reader_base_url.username
            )
            with owner_engine.begin() as connection:
                connection.execute(
                    text(
                        f'REVOKE CREATE ON SCHEMA "{schema_name}" '
                        "FROM PUBLIC"
                    )
                )
                connection.execute(
                    text(
                        f'GRANT USAGE ON SCHEMA "{schema_name}" '
                        f"TO {quoted_reader}"
                    )
                )
                connection.execute(
                    text(
                        f"GRANT SELECT ON TABLE {qualified_views} "
                        f"TO {quoted_reader}"
                    )
                )
            reader_query = dict(reader_base_url.query)
            reader_query["options"] = (
                f"-csearch_path={schema_name} "
                "-cdefault_transaction_read_only=on"
            )
            reader_url = reader_base_url.set(
                query=reader_query,
            ).render_as_string(hide_password=False)
            admin_engine.dispose()

            def cleanup_existing_reader() -> None:
                return None

            return reader_url, cleanup_existing_reader

    quoted_database = admin_engine.dialect.identifier_preparer.quote(
        database_name
    )
    with admin_engine.begin() as connection:
        connection.execute(
            text(
                f'CREATE ROLE "{role_name}" LOGIN '
                f"PASSWORD '{password}'"
            )
        )
        connection.execute(
            text(
                f'ALTER ROLE "{role_name}" '
                "SET default_transaction_read_only = on"
            )
        )
        connection.execute(
            text(
                f'ALTER ROLE "{role_name}" '
                f'SET search_path = "{schema_name}"'
            )
        )
        connection.execute(
            text(
                f"GRANT CONNECT ON DATABASE {quoted_database} "
                f'TO "{role_name}"'
            )
        )
        connection.execute(
            text(
                f'REVOKE CREATE ON SCHEMA "{schema_name}" FROM PUBLIC'
            )
        )
        connection.execute(
            text(
                f'GRANT USAGE ON SCHEMA "{schema_name}" '
                f'TO "{role_name}"'
            )
        )
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {qualified_views} "
                f'TO "{role_name}"'
            )
        )

    reader_url = admin_url.set(
        username=role_name,
        password=password,
        query={"options": f"-csearch_path={schema_name}"},
    ).render_as_string(hide_password=False)
    cleaned = False

    def cleanup() -> None:
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        try:
            with admin_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                connection.execute(
                    text(f'DROP OWNED BY "{role_name}"')
                )
                connection.execute(
                    text(f'DROP ROLE IF EXISTS "{role_name}"')
                )
        finally:
            admin_engine.dispose()

    atexit.register(cleanup)
    return reader_url, cleanup


def build_session() -> Session:
    engine, cleanup = _isolated_postgresql_engine("system_unit")

    class CleanupSession(Session):
        _cleanup_done = False

        def close(self) -> None:
            if self._cleanup_done:
                return
            try:
                super().close()
            finally:
                self._cleanup_done = True
                cleanup()
                atexit.unregister(cleanup)

    factory = sessionmaker(
        bind=engine,
        class_=CleanupSession,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    return factory()


def build_threadsafe_session_factory():
    engine, _cleanup = _isolated_postgresql_engine(
        "system_threadsafe"
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
