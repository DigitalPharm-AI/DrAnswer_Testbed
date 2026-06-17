from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from system_app.main import app
from system_app.migrations import run_migrations


def test_run_migrations_tracks_agent_jobs_version():
    engine = create_engine("sqlite:///:memory:", future=True)

    applied = run_migrations(engine)
    second_run = run_migrations(engine)

    inspector = inspect(engine)
    assert "agent_jobs" in inspector.get_table_names()
    assert "schema_migrations" in inspector.get_table_names()
    assert applied == [
        "20260429_0001_agent_jobs",
        "20260430_0001_simulation_patient_profiles",
        "20260518_0001_system_policy_overrides",
        "20260602_0001_missed_dose_flags",
    ]
    assert second_run == []

    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        versions = session.execute(text("SELECT version FROM schema_migrations")).scalars().all()
        assert versions == [
            "20260429_0001_agent_jobs",
            "20260430_0001_simulation_patient_profiles",
            "20260518_0001_system_policy_overrides",
            "20260602_0001_missed_dose_flags",
        ]


def test_health_details_returns_operational_shape():
    client = TestClient(app)

    response = client.get("/health/details")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert payload["database"]["ok"] is True
    assert "agent_server" in payload
    assert "api_key_configured" in payload["llm"]
    assert "required" in payload["internal_api"]
    assert "applied_versions" in payload["migrations"]
