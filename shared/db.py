from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker


def create_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    url = make_url(database_url)
    if url.drivername.startswith("sqlite"):
        engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        configure_sqlite_pragmas(engine)
    else:
        engine = create_engine(
            database_url,
            pool_pre_ping=True,
            future=True,
        )
    return engine, sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def create_sqlite_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    return create_session_factory(database_url)


def configure_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout = 30000")
        cursor.close()


def session_dependency(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as session:
        yield session
