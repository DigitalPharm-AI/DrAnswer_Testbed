from __future__ import annotations

import os
from collections.abc import Iterable

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.exc import DBAPIError

from agent_app.tools.backend_query import BackendQueryTools
from shared.backend_read_contract import (
    BACKEND_READ_CONTRACT_VERSION,
    BACKEND_READ_VIEW_COLUMNS,
    BACKEND_READ_VIEW_DEFINITIONS,
)
from system_app.migrations import ensure_backend_read_views
from system_app.models import Base

POSTGRES_BOUNDARY_TEST_DATABASE_URL = os.getenv(
    "BACKEND_POSTGRES_BOUNDARY_TEST_DATABASE_URL",
    "",
).strip()

# These identifiers are deliberately fixed and test-specific. The test refuses
# to run if either already exists, so cleanup can never remove a pre-existing
# role or schema from a developer-provided PostgreSQL database.
TEST_SCHEMA = "dranswer_v13_boundary_test"
TEST_READER_ROLE = "dranswer_v13_boundary_reader"
TEST_READER_PASSWORD = "dranswer-v13-boundary-test-only"


def _postgres_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        pytest.fail(
            "BACKEND_POSTGRES_BOUNDARY_TEST_DATABASE_URL must use PostgreSQL"
        )
    return url


def _url_with_search_path(url: URL) -> URL:
    return url.update_query_dict(
        {
            **dict(url.query),
            "options": f"-csearch_path={TEST_SCHEMA}",
        }
    )


def _reader_url(admin_url: URL) -> URL:
    return _url_with_search_path(
        admin_url.set(
            username=TEST_READER_ROLE,
            password=TEST_READER_PASSWORD,
        )
    )


def _source_tables() -> tuple[str, ...]:
    names = {
        str(table_name)
        for definition in BACKEND_READ_VIEW_DEFINITIONS.values()
        for table_name in dict(definition["sources"])
    }
    return tuple(sorted(names))


def _assert_statements_denied(engine: Engine, statements: Iterable[str]) -> None:
    for statement in statements:
        # A fresh connection ensures the failed PostgreSQL transaction is
        # rolled back before the next negative permission assertion.
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text(statement))


@pytest.mark.skipif(
    not POSTGRES_BOUNDARY_TEST_DATABASE_URL,
    reason="BACKEND_POSTGRES_BOUNDARY_TEST_DATABASE_URL is not set",
)
def test_postgres_reader_role_can_only_select_versioned_backend_views() -> None:
    """Exercise the real PostgreSQL role boundary without storing user data."""

    admin_url = _postgres_url(POSTGRES_BOUNDARY_TEST_DATABASE_URL)
    admin_engine = create_engine(admin_url, pool_pre_ping=True, future=True)
    owner_engine: Engine | None = None
    reader_engine: Engine | None = None
    query_tools: BackendQueryTools | None = None
    objects_created = False
    database_name = ""

    try:
        with admin_engine.begin() as connection:
            database_name = str(
                connection.execute(text("SELECT current_database()")).scalar_one()
            )
            schema_exists = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_namespace WHERE nspname = :schema_name"
                        ")"
                    ),
                    {"schema_name": TEST_SCHEMA},
                ).scalar_one()
            )
            role_exists = bool(
                connection.execute(
                    text(
                        "SELECT EXISTS ("
                        "SELECT 1 FROM pg_roles WHERE rolname = :role_name"
                        ")"
                    ),
                    {"role_name": TEST_READER_ROLE},
                ).scalar_one()
            )
            if schema_exists or role_exists:
                pytest.fail(
                    "PostgreSQL boundary test identifiers already exist; "
                    "refusing destructive cleanup"
                )

            quoted_database = connection.dialect.identifier_preparer.quote(
                database_name
            )
            connection.execute(
                text(
                    f'CREATE ROLE "{TEST_READER_ROLE}" '
                    f"LOGIN PASSWORD '{TEST_READER_PASSWORD}'"
                )
            )
            connection.execute(
                text(
                    f'ALTER ROLE "{TEST_READER_ROLE}" '
                    "SET default_transaction_read_only = on"
                )
            )
            connection.execute(
                text(
                    f'ALTER ROLE "{TEST_READER_ROLE}" '
                    f'SET search_path = "{TEST_SCHEMA}"'
                )
            )
            connection.execute(text(f'CREATE SCHEMA "{TEST_SCHEMA}"'))
            connection.execute(
                text(
                    f'GRANT CONNECT ON DATABASE {quoted_database} '
                    f'TO "{TEST_READER_ROLE}"'
                )
            )
        objects_created = True

        owner_engine = create_engine(
            _url_with_search_path(admin_url),
            pool_pre_ping=True,
            future=True,
        )
        Base.metadata.create_all(bind=owner_engine)
        assert ensure_backend_read_views(owner_engine) is True

        view_names = tuple(BACKEND_READ_VIEW_COLUMNS)
        source_tables = _source_tables()
        with owner_engine.begin() as connection:
            connection.execute(
                text(f'REVOKE CREATE ON SCHEMA "{TEST_SCHEMA}" FROM PUBLIC')
            )
            connection.execute(
                text(
                    f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA '
                    f'"{TEST_SCHEMA}" FROM PUBLIC'
                )
            )
            connection.execute(
                text(
                    f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA '
                    f'"{TEST_SCHEMA}" FROM PUBLIC'
                )
            )
            connection.execute(
                text(
                    f'GRANT USAGE ON SCHEMA "{TEST_SCHEMA}" '
                    f'TO "{TEST_READER_ROLE}"'
                )
            )
            qualified_views = ", ".join(
                f'"{TEST_SCHEMA}"."{view_name}"' for view_name in view_names
            )
            connection.execute(
                text(
                    f"GRANT SELECT ON TABLE {qualified_views} "
                    f'TO "{TEST_READER_ROLE}"'
                )
            )

        with admin_engine.connect() as connection:
            role = connection.execute(
                text(
                    """
                    SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls,
                           COALESCE(rolconfig, ARRAY[]::text[]) AS rolconfig
                    FROM pg_roles
                    WHERE rolname = :role_name
                    """
                ),
                {"role_name": TEST_READER_ROLE},
            ).mappings().one()
            assert role["rolsuper"] is False
            assert role["rolcreaterole"] is False
            assert role["rolcreatedb"] is False
            assert role["rolbypassrls"] is False
            assert "default_transaction_read_only=on" in set(role["rolconfig"])
            assert connection.execute(
                text(
                    "SELECT has_schema_privilege("
                    ":role_name, :schema_name, 'USAGE'"
                    ")"
                ),
                {
                    "role_name": TEST_READER_ROLE,
                    "schema_name": TEST_SCHEMA,
                },
            ).scalar_one() is True
            assert connection.execute(
                text(
                    "SELECT has_schema_privilege("
                    ":role_name, :schema_name, 'CREATE'"
                    ")"
                ),
                {
                    "role_name": TEST_READER_ROLE,
                    "schema_name": TEST_SCHEMA,
                },
            ).scalar_one() is False
            for view_name in view_names:
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        ":role_name, :relation_name, 'SELECT'"
                        ")"
                    ),
                    {
                        "role_name": TEST_READER_ROLE,
                        "relation_name": f"{TEST_SCHEMA}.{view_name}",
                    },
                ).scalar_one() is True
            for table_name in source_tables:
                assert connection.execute(
                    text(
                        "SELECT has_table_privilege("
                        ":role_name, :relation_name, 'SELECT'"
                        ")"
                    ),
                    {
                        "role_name": TEST_READER_ROLE,
                        "relation_name": f"{TEST_SCHEMA}.{table_name}",
                    },
                ).scalar_one() is False

        reader_engine = create_engine(
            _reader_url(admin_url),
            pool_pre_ping=True,
            future=True,
        )
        with reader_engine.connect() as connection:
            assert (
                str(
                    connection.execute(
                        text("SHOW transaction_read_only")
                    ).scalar_one()
                ).lower()
                == "on"
            )
            for view_name in view_names:
                connection.execute(
                    text(f'SELECT * FROM "{view_name}" WHERE FALSE')
                )

        query_tools = BackendQueryTools(
            _reader_url(admin_url).render_as_string(hide_password=False)
        )
        contract = query_tools.verify_contract()
        assert contract["server_version"]
        assert {
            key: value
            for key, value in contract.items()
            if key != "server_version"
        } == {
            "ok": True,
            "contract_version": BACKEND_READ_CONTRACT_VERSION,
            "dialect": "postgresql",
            "read_only": True,
            "views": sorted(BACKEND_READ_VIEW_COLUMNS),
        }

        _assert_statements_denied(
            reader_engine,
            (
                f'SELECT * FROM "{table_name}" WHERE FALSE'
                for table_name in source_tables
            ),
        )
        _assert_statements_denied(
            reader_engine,
            (
                'INSERT INTO "chat_messages" DEFAULT VALUES',
                'UPDATE "chat_messages" SET content = content WHERE FALSE',
                'DELETE FROM "chat_messages" WHERE FALSE',
                'TRUNCATE TABLE "chat_messages"',
                (
                    'UPDATE "ai_v13_chat_messages" '
                    "SET content = content WHERE FALSE"
                ),
                'DELETE FROM "ai_v13_chat_messages" WHERE FALSE',
                'DROP VIEW "ai_v13_chat_messages"',
                'CREATE TABLE "ai_v13_boundary_forbidden" (id INTEGER)',
            ),
        )
        with owner_engine.connect() as connection:
            assert (
                inspect(connection).has_table("ai_v13_boundary_forbidden")
                is False
            )
    finally:
        if query_tools is not None:
            query_tools.engine.dispose()
        if reader_engine is not None:
            reader_engine.dispose()
        if owner_engine is not None:
            owner_engine.dispose()
        if objects_created:
            with admin_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as connection:
                quoted_database = connection.dialect.identifier_preparer.quote(
                    database_name
                )
                connection.execute(
                    text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE')
                )
                connection.execute(
                    text(
                        f'REVOKE CONNECT ON DATABASE {quoted_database} '
                        f'FROM "{TEST_READER_ROLE}"'
                    )
                )
                connection.execute(
                    text(f'DROP ROLE IF EXISTS "{TEST_READER_ROLE}"')
                )
        admin_engine.dispose()
