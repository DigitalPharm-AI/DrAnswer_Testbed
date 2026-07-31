from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


@dataclass(frozen=True)
class DatabaseEngineConfig:
    """Runtime connection-pool and timeout policy for one database role."""

    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout_seconds: float = 30.0
    pool_recycle_seconds: int = 1_800
    statement_timeout_ms: int = 30_000
    lock_timeout_ms: int = 5_000

    def __post_init__(self) -> None:
        if self.pool_size < 1:
            raise ValueError("database_pool_size_must_be_positive")
        if self.max_overflow < 0:
            raise ValueError("database_max_overflow_must_be_non_negative")
        if self.pool_timeout_seconds <= 0:
            raise ValueError("database_pool_timeout_must_be_positive")
        if self.pool_recycle_seconds < 0:
            raise ValueError("database_pool_recycle_must_be_non_negative")
        if self.statement_timeout_ms < 0:
            raise ValueError("database_statement_timeout_must_be_non_negative")
        if self.lock_timeout_ms < 0:
            raise ValueError("database_lock_timeout_must_be_non_negative")


def create_database_engine(
    database_url: str,
    *,
    config: DatabaseEngineConfig | None = None,
    read_only: bool = False,
) -> Engine:
    """Create a PostgreSQL runtime engine without exposing its URL.

    SQLite is intentionally excluded so a local file can never become an
    application fallback.
    """

    url = make_url(database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError(
            f"database_postgresql_required:{url.drivername}"
        )

    resolved = config or DatabaseEngineConfig()
    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        pool_size=resolved.pool_size,
        max_overflow=resolved.max_overflow,
        pool_timeout=resolved.pool_timeout_seconds,
        pool_recycle=resolved.pool_recycle_seconds,
        future=True,
    )
    configure_postgresql_session(
        engine,
        statement_timeout_ms=resolved.statement_timeout_ms,
        lock_timeout_ms=resolved.lock_timeout_ms,
        read_only=read_only,
    )
    return engine


def create_session_factory(
    database_url: str,
    *,
    config: DatabaseEngineConfig | None = None,
) -> tuple[Engine, sessionmaker[Session]]:
    engine = create_database_engine(
        database_url,
        config=config,
        read_only=False,
    )
    return engine, sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def configure_postgresql_session(
    engine: Engine,
    *,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
    read_only: bool,
) -> None:
    @event.listens_for(engine, "connect")
    def _configure_postgresql(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute(
            "SET SESSION statement_timeout = "
            f"'{int(statement_timeout_ms)}ms'"
        )
        cursor.execute(
            "SET SESSION lock_timeout = "
            f"'{int(lock_timeout_ms)}ms'"
        )
        if read_only:
            cursor.execute(
                "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY"
            )
        cursor.close()
        # psycopg starts a transaction for SET. Commit before the connection
        # is handed to SQLAlchemy so the first application transaction is not
        # accidentally nested in setup work.
        dbapi_connection.commit()


def database_server_identity(engine: Engine) -> dict[str, str]:
    """Return a secret-free PostgreSQL identity for readiness payloads."""

    with engine.connect() as connection:
        dialect = connection.dialect.name
        if dialect != "postgresql":
            raise RuntimeError(
                f"database_postgresql_required:{dialect}"
            )
        version_info = connection.dialect.server_version_info or ()
        version = ".".join(str(part) for part in version_info)
        if not version:
            version = "unknown"
        return {
            "dialect": dialect,
            "server_version": version,
        }


def session_dependency(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
