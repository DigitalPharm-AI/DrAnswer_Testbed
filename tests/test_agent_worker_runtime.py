from __future__ import annotations

import agent_app.worker_main as worker_main


def test_worker_main_initializes_queue_and_runs_worker(monkeypatch):
    events: list[object] = []

    class FakeSettings:
        def require_internal_api_token_in_production(self) -> None:
            events.append("settings_checked")

    class FakeSession:
        def __init__(self, _engine):
            events.append("session_created")

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            return False

        def commit(self) -> None:
            events.append("session_committed")

    def fake_worker(stop_event, orchestrator):
        events.append(("worker_started", orchestrator, stop_event.is_set()))
        stop_event.set()

    monkeypatch.setattr(worker_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(worker_main.Base.metadata, "create_all", lambda bind: events.append(("schema_created", bind is worker_main.engine)))
    monkeypatch.setattr(worker_main, "run_migrations", lambda engine: events.append(("migrations_run", engine is worker_main.engine)) or [])
    monkeypatch.setattr(worker_main, "Session", FakeSession)
    monkeypatch.setattr(worker_main, "reset_running_async_tasks", lambda session: events.append(("tasks_reset", session is not None)) or 0)
    monkeypatch.setattr(worker_main, "create_orchestrator", lambda: "fake-orchestrator")
    monkeypatch.setattr(worker_main, "async_task_worker", fake_worker)
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_args, **_kwargs: None)

    worker_main.main()

    assert events == [
        "settings_checked",
        ("schema_created", True),
        ("migrations_run", True),
        "session_created",
        ("tasks_reset", True),
        "session_committed",
        ("worker_started", "fake-orchestrator", False),
    ]
