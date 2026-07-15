from __future__ import annotations

import asyncio
import inspect
import threading

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import system_app.db as system_db
import system_app.main as system_main
from system_app.models import Base, ChatMessage, Notification
from system_app.services.simulation import ensure_base_data


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


def test_chat_and_agent_callback_db_routes_run_in_threadpool():
    route_modules = {"system_app.routes.chat", "system_app.routes.agent_async_api"}

    def nested_endpoints(routes):
        for route in routes:
            original_router = getattr(route, "original_router", None)
            children = getattr(route, "routes", None) or getattr(original_router, "routes", None)
            if children:
                yield from nested_endpoints(children)
            elif endpoint := getattr(route, "endpoint", None):
                yield endpoint

    endpoints = [endpoint for endpoint in nested_endpoints(system_main.app.routes) if getattr(endpoint, "__module__", "") in route_modules]

    assert endpoints
    assert all(not inspect.iscoroutinefunction(endpoint) for endpoint in endpoints)


def test_chat_write_lock_wait_does_not_block_health(monkeypatch):
    message = "event loop lock isolation test"
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    with system_db.SessionLocal() as session:
        ensure_base_data(session)
        session.commit()

    monkeypatch.setattr(system_main, "system_event_worker", lambda *_args: None)

    def hold_write_lock():
        with system_main.write_lock:
            lock_acquired.set()
            release_lock.wait(timeout=1.5)

    holder = threading.Thread(target=hold_write_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=1)

    async def submit_chat_while_health_stays_responsive():
        transport = httpx.ASGITransport(app=system_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            post_task = asyncio.create_task(client.post("/chat/system", data={"message": message}))
            await asyncio.sleep(0.05)
            health_response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            release_lock.set()
            post_response = await asyncio.wait_for(post_task, timeout=3)
            return health_response, post_response

    try:
        health_response, post_response = asyncio.run(submit_chat_while_health_stays_responsive())
        assert health_response.is_success
        assert post_response.is_success
    finally:
        release_lock.set()
        holder.join(timeout=2)
        with system_db.SessionLocal() as session:
            session.query(Notification).filter(Notification.body.contains(message)).delete(synchronize_session=False)
            session.query(ChatMessage).filter(ChatMessage.content == message).delete(synchronize_session=False)
            session.commit()
