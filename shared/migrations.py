from __future__ import annotations

from collections.abc import Callable, Sequence

from sqlalchemy import Engine, inspect, text
from sqlalchemy.engine import Connection

Migration = tuple[str, str]
MigrationSkipper = Callable[[Connection, str], bool]


def table_columns(connection: Connection, table_name: str) -> set[str]:
    try:
        return {str(column["name"]) for column in inspect(connection).get_columns(table_name)}
    except Exception:
        if connection.dialect.name != "sqlite":
            raise
        rows = connection.execute(text(f"PRAGMA table_info({table_name})")).mappings().all()
        return {str(row["name"]) for row in rows}


def run_sql_migrations(
    engine: Engine,
    migrations: Sequence[Migration],
    should_skip: MigrationSkipper | None = None,
) -> list[str]:
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
