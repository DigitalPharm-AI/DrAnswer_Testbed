from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.pool import NullPool

Migration = tuple[str, str]
MigrationSkipper = Callable[[Connection, str], bool]


@contextmanager
def migration_advisory_lock(
    engine: Engine,
    namespace: str,
):
    """Serialize a complete PostgreSQL schema migration across processes."""

    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            f"migration_postgresql_required:{engine.dialect.name}"
        )

    lock_key = _advisory_lock_key(namespace)
    # Keep the lock connection outside the runtime pool. This lets a migration
    # engine use pool_size=1 without deadlocking when schema work checks out
    # its own connection.
    lock_engine = create_engine(
        engine.url,
        poolclass=NullPool,
        future=True,
    )
    try:
        with lock_engine.connect() as connection:
            connection.execute(
                text("SELECT pg_advisory_lock(:lock_key)"),
                {"lock_key": lock_key},
            )
            connection.commit()
            try:
                yield
            finally:
                connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_key)"),
                    {"lock_key": lock_key},
                )
                connection.commit()
    finally:
        lock_engine.dispose()


def migration_status(
    engine: Engine,
    expected_versions: Sequence[str],
) -> dict[str, Any]:
    expected = tuple(str(version) for version in expected_versions)
    with engine.connect() as connection:
        table_names = set(inspect(connection).get_table_names())
        if "schema_migrations" not in table_names:
            return {
                "current": False,
                "applied_versions": [],
                "missing_versions": list(expected),
                "latest_version": None,
            }
        applied = tuple(
            str(version)
            for version in connection.execute(
                text(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version"
                )
            ).scalars()
        )
    missing = tuple(version for version in expected if version not in applied)
    return {
        "current": not missing,
        "applied_versions": list(applied),
        "missing_versions": list(missing),
        "latest_version": applied[-1] if applied else None,
    }


def _advisory_lock_key(namespace: str) -> int:
    digest = hashlib.sha256(namespace.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


def table_columns(connection: Connection, table_name: str) -> set[str]:
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return set()
    return {
        str(column["name"])
        for column in inspector.get_columns(table_name)
    }


def run_sql_migrations(
    engine: Engine,
    migrations: Sequence[Migration],
    should_skip: MigrationSkipper | None = None,
) -> list[str]:
    if engine.dialect.name != "postgresql":
        raise RuntimeError(
            f"migration_postgresql_required:{engine.dialect.name}"
        )
    applied_now: list[str] = []
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(80) NOT NULL PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        applied = set(connection.execute(text("SELECT version FROM schema_migrations")).scalars().all())
        for version, sql in migrations:
            if version in applied:
                continue
            if should_skip is not None and should_skip(connection, version):
                connection.execute(text("INSERT INTO schema_migrations (version) VALUES (:version)"), {"version": version})
                continue
            connection.execute(text(sql))
            connection.execute(text("INSERT INTO schema_migrations (version) VALUES (:version)"), {"version": version})
            applied_now.append(version)
    return applied_now
