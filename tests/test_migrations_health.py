import asyncio

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from system_app.main import app
from system_app.migrations import run_migrations
from system_app.services import health as health_service


def test_run_migrations_tracks_agent_jobs_version():
    engine = create_engine("sqlite:///:memory:", future=True)

    applied = run_migrations(engine)
    second_run = run_migrations(engine)

    inspector = inspect(engine)
    assert "agent_jobs" in inspector.get_table_names()
    assert "nutrition_ontology_nodes" in inspector.get_table_names()
    assert "nutrition_ontology_triples" in inspector.get_table_names()
    assert "nutrition_patient_preference_triples" in inspector.get_table_names()
    assert "schema_migrations" in inspector.get_table_names()
    assert applied == [
        "20260429_0001_agent_jobs",
        "20260430_0001_simulation_patient_profiles",
        "20260518_0001_system_policy_overrides",
        "20260602_0001_missed_dose_flags",
        "20260618_0001_nutrition_profiles",
        "20260618_0002_nutrition_meals",
        "20260618_0003_nutrition_foods",
        "20260618_0004_daily_nutrition_checks",
        "20260618_0005_nutrition_meal_index",
        "20260618_0006_nutrition_food_index",
        "20260618_0007_daily_nutrition_index",
        "20260619_0001_agent_run_traces",
        "20260619_0002_agent_run_steps",
        "20260619_0003_agent_trace_indexes",
        "20260619_0004_agent_run_steps_trace",
        "20260622_0001_nutrition_ontology_nodes",
        "20260622_0002_nutrition_ontology_triples",
        "20260622_0003_nutrition_patient_preference_triples",
        "20260622_0004_nutrition_ontology_indexes",
        "20260622_0005_nutrition_patient_preference_indexes",
        "20260701_0001_nutrition_food_ref",
        "20260701_0002_nutrition_food_ref_index",
            "20260710_0001_side_effect_records",
            "20260710_0002_side_effect_records_patient_created",
            "20260710_0003_side_effect_records_suspected_medication",
            "20260715_0001_mutation_confirmations",
            "20260715_0002_mutation_confirmations_patient_status",
            "20260715_0003_mutation_confirmations_fingerprint",
            "20260725_0001_backend_api_requests",
            "20260725_0002_backend_api_request_lookup",
            "20260725_0003_chat_conversation_lookup",
            "20260725_0004_chat_assistant_request_unique",
            "20260725_0005_external_public_ids",
            "20260726_0001_medication_plan_submission_id",
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
            "20260618_0001_nutrition_profiles",
            "20260618_0002_nutrition_meals",
            "20260618_0003_nutrition_foods",
            "20260618_0004_daily_nutrition_checks",
            "20260618_0005_nutrition_meal_index",
            "20260618_0006_nutrition_food_index",
            "20260618_0007_daily_nutrition_index",
            "20260619_0001_agent_run_traces",
            "20260619_0002_agent_run_steps",
            "20260619_0003_agent_trace_indexes",
            "20260619_0004_agent_run_steps_trace",
            "20260622_0001_nutrition_ontology_nodes",
            "20260622_0002_nutrition_ontology_triples",
            "20260622_0003_nutrition_patient_preference_triples",
            "20260622_0004_nutrition_ontology_indexes",
            "20260622_0005_nutrition_patient_preference_indexes",
            "20260701_0001_nutrition_food_ref",
            "20260701_0002_nutrition_food_ref_index",
        "20260710_0001_side_effect_records",
        "20260710_0002_side_effect_records_patient_created",
        "20260710_0003_side_effect_records_suspected_medication",
            "20260715_0001_mutation_confirmations",
            "20260715_0002_mutation_confirmations_patient_status",
            "20260715_0003_mutation_confirmations_fingerprint",
            "20260725_0001_backend_api_requests",
            "20260725_0002_backend_api_request_lookup",
            "20260725_0003_chat_conversation_lookup",
            "20260725_0004_chat_assistant_request_unique",
            "20260725_0005_external_public_ids",
            "20260726_0001_medication_plan_submission_id",
        ]


def test_health_details_returns_operational_shape():
    client = TestClient(app)

    response = client.get("/health/details")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] in {"ok", "degraded"}
    assert "warnings" in payload
    assert payload["database"]["ok"] is True
    assert "agent_server" in payload
    assert "agent_async" in payload
    assert "worker_available" in payload["agent_async"]
    assert "api_key_configured" in payload["llm"]
    assert "status" in payload["llm"]
    assert "required" in payload["internal_api"]
    assert "applied_versions" in payload["migrations"]
    assert payload["budgets"]["load"]["concurrency_target"] >= 1
    assert "token_prices_configured" in payload["budgets"]["cost"]


def test_rule_based_provider_is_not_runtime_supported():
    class DummySettings:
        llm_provider = "rule_based"
        app_env = "development"

    assert health_service._llm_provider_supported(DummySettings()) is False
    assert health_service._llm_credentials_required(DummySettings()) is False


def test_rule_based_provider_is_supported_without_credentials_in_testbed():
    class DummySettings:
        llm_provider = "rule_based"
        app_env = "testbed"

    assert health_service._llm_provider_supported(DummySettings()) is True
    assert health_service._llm_credentials_required(DummySettings()) is False


def test_bedrock_provider_requires_credentials():
    class DummySettings:
        llm_provider = "bedrock_anthropic"

    assert health_service._llm_credentials_required(DummySettings()) is True


def test_agent_async_health_summarizes_worker_queue(monkeypatch):
    captured: dict[str, object] = {}

    class DummyAsyncClient:
        def __init__(self, *, timeout: float, **_: object) -> None:
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get(self, url: str, *, headers=None):
            captured.update({"url": url, "headers": headers, "timeout": self.timeout})
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "counts": {"pending": 2, "dead": 1, "done": 5},
                    "active_count": 2,
                    "workers": [
                        {
                            "worker_id": "worker-a",
                            "status": "running",
                            "last_error": "pytest private async worker error peanut allergy token=secret-value",
                        },
                        {"worker_id": "worker-b", "status": "stale"},
                    ],
                },
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(health_service.httpx, "AsyncClient", DummyAsyncClient)

    payload = asyncio.run(health_service._agent_async_health("http://agent.test/", "agent-token"))

    assert captured["url"] == "http://agent.test/agent/async/tasks/status"
    assert captured["headers"] == {"X-Internal-Api-Token": "agent-token"}
    assert captured["timeout"] == 1.0
    assert payload["reachable"] is True
    assert payload["pending_count"] == 2
    assert payload["dead_count"] == 1
    assert payload["worker_count"] == 2
    assert payload["running_worker_count"] == 1
    assert payload["stale_worker_count"] == 1
    assert payload["worker_available"] is True
    assert payload["warnings"] == ["dead_tasks_present"]
    assert payload["workers"][0]["last_error"].startswith("clinical text redacted")
    assert "pytest private async worker error" not in str(payload)
    assert "secret-value" not in str(payload)
