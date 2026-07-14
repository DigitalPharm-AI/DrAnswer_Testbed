from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import system_app.db as system_db
import system_app.main as system_main
from system_app.models import Base


def test_system_polling_routes_return_connections_under_concurrency(tmp_path, monkeypatch):
    database_path = tmp_path / "system-route-concurrency.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
        pool_size=2,
        max_overflow=0,
        pool_timeout=1,
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(system_db, "SessionLocal", session_factory)

    async def send_concurrent_requests():
        transport = httpx.ASGITransport(app=system_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            requests = [client.get("/partials/time-bar") for _ in range(50)]
            return await asyncio.wait_for(asyncio.gather(*requests), timeout=5)

    try:
        responses = asyncio.run(send_concurrent_requests())
        assert all(response.is_success for response in responses)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()
