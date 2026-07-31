from __future__ import annotations

import os
from types import SimpleNamespace

import agent_app.worker_main as worker_main


def test_worker_main_initializes_queue_and_runs_worker(monkeypatch):
    events: list[object] = []

    class FakeSettings:
        def require_internal_api_token(self) -> str:
            events.append("settings_checked")
            return "test-internal-token"

        def require_backend_read_database_url(self) -> None:
            events.append("backend_read_settings_checked")

        def require_agent_postgresql(self) -> None:
            events.append("agent_database_settings_checked")

        def require_backend_read_postgresql(self) -> None:
            events.append("backend_read_database_settings_checked")

    class FakeSession:
        def __init__(self, _engine):
            events.append("session_created")

        def __enter__(self):
            return self

        def __exit__(self, _exc_type, _exc, _tb) -> bool:
            return False

        def commit(self) -> None:
            events.append("session_committed")

    def fake_worker(stop_event, orchestrator, backend_queries):
        events.append(
            (
                "worker_started",
                orchestrator,
                backend_queries,
                stop_event.is_set(),
            )
        )
        stop_event.set()

    monkeypatch.setattr(worker_main, "get_settings", lambda: FakeSettings())
    monkeypatch.setattr(
        worker_main,
        "verify_agent_schema_current",
        lambda engine: events.append(
            ("schema_verified", engine is worker_main.engine)
        ),
    )
    monkeypatch.setattr(worker_main, "Session", FakeSession)
    monkeypatch.setattr(worker_main, "reset_running_async_tasks", lambda session: events.append(("tasks_reset", session is not None)) or 0)
    monkeypatch.setattr(
        worker_main,
        "create_runtime_components",
        lambda: SimpleNamespace(
            orchestrator="fake-orchestrator",
            tool_server=SimpleNamespace(backend_queries="fake-backend-queries"),
        ),
    )
    monkeypatch.setattr(worker_main, "async_task_worker", fake_worker)
    monkeypatch.setattr(worker_main.signal, "signal", lambda *_args, **_kwargs: None)

    worker_main.main()

    assert events == [
        "settings_checked",
        "backend_read_settings_checked",
        "agent_database_settings_checked",
        "backend_read_database_settings_checked",
        ("schema_verified", True),
        "session_created",
        ("tasks_reset", True),
        "session_committed",
        (
            "worker_started",
            "fake-orchestrator",
            "fake-backend-queries",
            False,
        ),
    ]


def test_worker_main_publishes_and_cleans_runtime_pid_file(
    tmp_path,
    monkeypatch,
) -> None:
    pid_path = tmp_path / "agent-worker.runtime.pid"
    observed: list[str] = []

    def fake_run_worker() -> None:
        observed.append(pid_path.read_text(encoding="ascii"))

    monkeypatch.setenv("DA_DRUG_RUNTIME_PID_FILE", str(pid_path))
    monkeypatch.setattr(worker_main, "_run_worker", fake_run_worker)

    worker_main.main()

    assert observed == [str(os.getpid())]
    assert not pid_path.exists()
