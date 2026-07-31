from __future__ import annotations

from pathlib import Path

from system_app.models import Base


def test_backend_has_no_duplicate_agent_trace_tables_or_read_api():
    assert "agent_run_traces" not in Base.metadata.tables
    assert "agent_run_steps" not in Base.metadata.tables

    route_source = (
        Path(__file__).resolve().parents[1]
        / "system_app"
        / "routes"
        / "agent_async_api.py"
    ).read_text(encoding="utf-8")
    assert '"/traces"' not in route_source
    assert '"/traces/{trace_id}"' not in route_source
    assert '"/job-results"' not in route_source
    assert '"/failures"' not in route_source
    assert '"/missed-dose-results"' in route_source


def test_backend_does_not_persist_agent_trace_ids_in_business_metadata():
    repository_root = Path(__file__).resolve().parents[1]
    response_source = (
        repository_root
        / "system_app"
        / "services"
        / "agent_response_service.py"
    ).read_text(encoding="utf-8")
    worker_source = (
        repository_root
        / "system_app"
        / "services"
        / "workers.py"
    ).read_text(encoding="utf-8")
    client_source = (
        repository_root
        / "system_app"
        / "services"
        / "agent_client.py"
    ).read_text(encoding="utf-8")

    assert '"trace_id": response.trace_id' not in response_source
    assert '"trace_id": getattr(error' not in worker_source
    assert 'trace_id=payload.get("trace_id")' not in client_source
    assert 'existing_metadata.pop("trace_id", None)' in response_source
    assert 'metadata.pop("trace_id", None)' in worker_source
