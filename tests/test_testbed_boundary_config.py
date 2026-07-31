from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from tools import verify_testbed_contract as boundary_contract
from tools.verify_testbed_contract import (
    _compose_boundary_violations,
    _dev_compose_boundary_violations,
    _env_boundary_violations,
    load_yaml,
    read_env_file,
    verify_config,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"
AGENT_CREDENTIAL_ENV_FILE = ".env.agent_app.secret"


def test_committed_testbed_boundary_configuration_passes() -> None:
    result = verify_config(
        compose_path=PROJECT_ROOT / "docker-compose.yml",
        dev_compose_path=PROJECT_ROOT / "docker-compose.dev.yml",
        env_path=PROJECT_ROOT / ".env.9000.example",
    )

    assert result["ok"] is True, result["violations"]


def test_compose_declares_backend_agent_and_worker_without_legacy_phr_process() -> None:
    compose = load_yaml(PROJECT_ROOT / "docker-compose.yml")
    services = compose["services"]

    assert "system_app.main:app" in services["system-app"]["command"]
    assert "agent_app.main:app" in services["agent-app"]["command"]
    assert (
        services["system-migrate"]["command"]
        == "python -m system_app.migrate"
    )
    assert services["agent-migrate"]["command"] == "python -m agent_app.migrate"
    assert services["agent-worker"]["command"] == "python -m agent_app.worker_main"
    assert "phr-app" not in services
    assert services["agent-app"]["environment"]["AGENT_EMBEDDED_WORKER_ENABLED"] == "false"
    assert services["agent-app"]["depends_on"]["system-app"]["condition"] == "service_healthy"
    assert (
        services["agent-app"]["depends_on"]["agent-migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert (
        services["system-app"]["depends_on"]["system-migrate"]["condition"]
        == "service_completed_successfully"
    )
    assert "agent-app" not in (services["system-app"].get("depends_on") or {})
    assert services["agent-worker"]["depends_on"]["agent-app"]["condition"] == "service_healthy"
    assert "/health/ready" in " ".join(services["agent-app"]["healthcheck"]["test"])
    assert "SYSTEM_DATABASE_URL" not in services["system-app"]["environment"]
    assert "AGENT_DATABASE_URL" not in services["agent-app"]["environment"]
    assert (
        "BACKEND_READ_DATABASE_URL"
        not in services["agent-app"]["environment"]
    )
    assert any(
        item.get("path") == ".env.agent_app"
        for item in services["agent-app"]["env_file"]
    )
    assert {
        item.get("path")
        for item in services["agent-app"]["env_file"]
    } == {".env.agent_app", AGENT_CREDENTIAL_ENV_FILE}
    assert {
        item.get("path")
        for item in services["agent-worker"]["env_file"]
    } == {".env.agent_app", AGENT_CREDENTIAL_ENV_FILE}
    for service_name in (
        "system-migrate",
        "system-app",
        "agent-migrate",
        "agent-langfuse-exporter",
    ):
        assert AGENT_CREDENTIAL_ENV_FILE not in {
            item.get("path")
            for item in services[service_name]["env_file"]
        }
        assert (
            services[service_name]["environment"][
                BEDROCK_BEARER_ENV_KEY
            ]
            == ""
        )
    for service_name in ("agent-app", "agent-worker"):
        assert (
            BEDROCK_BEARER_ENV_KEY
            not in services[service_name]["environment"]
        )
    assert any(
        item.get("path") == ".env.system_app"
        for item in services["system-app"]["env_file"]
    )
    assert all(
        item.get("path") != ".env"
        for service in services.values()
        for item in service.get("env_file") or []
    )
    assert "phr-runtime" not in compose["volumes"]
    assert all(
        "PHR_BASE_URL" not in (service.get("environment") or {})
        and "PHR_DATABASE_URL" not in (service.get("environment") or {})
        for service in services.values()
    )


def test_compose_does_not_share_backend_runtime_storage_with_agent() -> None:
    compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.yml"))

    for service_name in ("agent-app", "agent-worker"):
        assert all(
            mount.get("source") != "system-runtime"
            for mount in compose["services"][service_name]["volumes"]
        )


def test_boundary_check_rejects_repository_root_dev_mount() -> None:
    dev_compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.dev.yml"))
    dev_compose["services"]["agent-app"]["volumes"] = [".:/app"]

    violations = _dev_compose_boundary_violations(dev_compose)

    assert any("must not mount the repository root" in item for item in violations)
    assert any("source mount must be read-only" in item for item in violations)


def test_boundary_check_rejects_cross_service_application_source_mount() -> None:
    dev_compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.dev.yml"))
    dev_compose["services"]["system-app"]["volumes"].append(
        {
            "type": "bind",
            "source": "./agent_app",
            "target": "/app/agent_app",
            "read_only": True,
        }
    )

    violations = _dev_compose_boundary_violations(dev_compose)

    assert any(
        "system-app must not mount another service application source ./agent_app"
        in item
        for item in violations
    )


def test_boundary_check_rejects_agent_secret_on_non_runtime_services() -> None:
    compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.yml"))
    services = compose["services"]
    services["agent-migrate"]["env_file"].append(
        {"path": AGENT_CREDENTIAL_ENV_FILE, "required": False}
    )
    services["system-app"]["environment"][BEDROCK_BEARER_ENV_KEY] = (
        "synthetic-sensitive-marker"
    )

    violations = _compose_boundary_violations(compose)

    assert any(
        "agent-migrate has unexpected env_file entries" in item
        for item in violations
    )
    assert any(
        f"system-app.{BEDROCK_BEARER_ENV_KEY}" in item
        for item in violations
    )
    assert "synthetic-sensitive-marker" not in repr(violations)


def test_boundary_check_requires_agent_secret_on_api_and_worker() -> None:
    compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.yml"))
    services = compose["services"]
    for service_name in ("agent-app", "agent-worker"):
        services[service_name]["env_file"] = [
            item
            for item in services[service_name]["env_file"]
            if item.get("path") != AGENT_CREDENTIAL_ENV_FILE
        ]

    violations = _compose_boundary_violations(compose)

    assert any(
        "agent-app is missing env_file entries" in item
        and AGENT_CREDENTIAL_ENV_FILE in item
        for item in violations
    )
    assert any(
        "agent-worker is missing env_file entries" in item
        and AGENT_CREDENTIAL_ENV_FILE in item
        for item in violations
    )


def test_committed_common_env_templates_exclude_bedrock_bearer() -> None:
    common_paths = (
        PROJECT_ROOT / ".env.9000.example",
        PROJECT_ROOT / ".env.agent_app.example",
        PROJECT_ROOT / ".env.system_app.example",
    )

    for path in common_paths:
        assert BEDROCK_BEARER_ENV_KEY not in read_env_file(path), path.name

    agent_secret = read_env_file(
        PROJECT_ROOT / ".env.agent_app.secret.example"
    )
    assert BEDROCK_BEARER_ENV_KEY in agent_secret


def test_common_9000_profile_rejects_bedrock_bearer_without_value_leak() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.example")
    marker = "synthetic-sensitive-marker"
    env[BEDROCK_BEARER_ENV_KEY] = marker

    violations = _env_boundary_violations(env)

    assert any(BEDROCK_BEARER_ENV_KEY in item for item in violations)
    assert marker not in repr(violations)


def test_boundary_check_rejects_shared_reader_role_and_tokens() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.example")
    env["BACKEND_READ_DATABASE_URL"] = env["SYSTEM_DATABASE_URL"]
    env["BACKEND_API_TOKEN"] = env["AGENT_SYNC_API_TOKEN"]

    violations = _env_boundary_violations(env)

    assert any("dedicated reader role" in item for item in violations)
    assert any("must be different" in item for item in violations)


def test_boundary_check_rejects_deterministic_provider_outside_testbed() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.example")
    env["LLM_PROVIDER"] = "deterministic_test"
    env["APP_ENV"] = "development"

    violations = _env_boundary_violations(env)

    assert any(
        "deterministic-test LLM_PROVIDER requires APP_ENV" in item
        for item in violations
    )


def test_boundary_check_accepts_postgresql_production_profile() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.example")
    env.update(
        {
            "APP_ENV": "production",
            "SYSTEM_DATABASE_URL": (
                "postgresql+psycopg://backend_app@db/"
                "dranswer_backend"
            ),
            "AGENT_DATABASE_URL": (
                "postgresql+psycopg://agent_app@db/"
                "dranswer_agent"
            ),
            "BACKEND_READ_DATABASE_URL": (
                "postgresql+psycopg://ai_reader@db/"
                "dranswer_backend"
            ),
            "SYSTEM_MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://backend_migration@db/"
                "dranswer_backend"
            ),
            "AGENT_MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://agent_migration@db/"
                "dranswer_agent"
            ),
            "SYSTEM_STARTUP_MIGRATIONS_ENABLED": "false",
            "AGENT_STARTUP_MIGRATIONS_ENABLED": "false",
            "LLM_PROVIDER": "bedrock_anthropic",
        }
    )

    assert _env_boundary_violations(env) == []


def test_boundary_check_rejects_sqlite_production_profile() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.example")
    env["APP_ENV"] = "production"
    env["SYSTEM_DATABASE_URL"] = "sqlite:///system.db"
    env["SYSTEM_MIGRATION_DATABASE_URL"] = "sqlite:///system.db"
    env["AGENT_DATABASE_URL"] = "sqlite:///agent.db"
    env["AGENT_MIGRATION_DATABASE_URL"] = "sqlite:///agent.db"
    env["BACKEND_READ_DATABASE_URL"] = "sqlite:///system.db"
    env["SYSTEM_STARTUP_MIGRATIONS_ENABLED"] = "false"
    env["AGENT_STARTUP_MIGRATIONS_ENABLED"] = "false"

    violations = _env_boundary_violations(env)

    assert any(
        "AGENT_DATABASE_URL must use PostgreSQL" in item
        for item in violations
    )
    assert any(
        "BACKEND_READ_DATABASE_URL must use PostgreSQL" in item
        for item in violations
    )


def test_real_service_playwright_uses_api_oracle_without_sqlite() -> None:
    validation_source = (
        PROJECT_ROOT
        / "tools"
        / "playwright_p0_real_service_validation.js"
    ).read_text(encoding="utf-8")
    runner_source = (
        PROJECT_ROOT / "tools" / "run_real_llm_playwright.ps1"
    ).read_text(encoding="utf-8")

    assert "backendNutritionEvidence" in validation_source
    assert "/api/ui/v1/nutrition" in validation_source
    assert "SYSTEM_DB_PATH" not in validation_source
    assert "sqlite3" not in validation_source
    assert "spawnSync" not in validation_source
    assert ".db" not in runner_source.lower()


def test_env_reader_merges_agent_credential_overlay_in_precedence_order(
    tmp_path: Path,
) -> None:
    common = tmp_path / "common.env"
    agent_secret = tmp_path / "agent-secret.env"
    common.write_text(
        "LLM_PROVIDER=bedrock_anthropic\n",
        encoding="utf-8",
    )
    agent_secret.write_text(
        "LLM_PROVIDER=bedrock_anthropic\n"
        f"{BEDROCK_BEARER_ENV_KEY}=synthetic-agent-secret\n",
        encoding="utf-8",
    )

    common_env = read_env_file(common)
    env = read_env_file(f"{common},{agent_secret}")

    assert BEDROCK_BEARER_ENV_KEY not in common_env
    assert env["LLM_PROVIDER"] == "bedrock_anthropic"
    assert env[BEDROCK_BEARER_ENV_KEY] == "synthetic-agent-secret"


def test_ci_runs_real_postgres_reader_role_boundary() -> None:
    workflow = load_yaml(
        PROJECT_ROOT / ".github" / "workflows" / "testbed-boundary-contract.yml"
    )
    job = workflow["jobs"]["boundary-contract"]
    postgres = job["services"]["postgres"]

    assert postgres["image"] == "postgres:16"
    assert postgres["env"]["POSTGRES_DB"] == "dranswer_boundary"
    assert "pg_isready" in postgres["options"]
    assert (
        job["env"]["BACKEND_POSTGRES_BOUNDARY_TEST_DATABASE_URL"]
        == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/dranswer_boundary"
    )
    assert (
        job["env"]["AGENT_POSTGRES_TEST_DATABASE_URL"]
        == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/dranswer_agent_test"
    )
    assert (
        job["env"]["SYSTEM_POSTGRES_TEST_DATABASE_URL"]
        == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/dranswer_system_test"
    )
    assert (
        job["env"]["CONTRACT_POSTGRES_TEST_DATABASE_URL"]
        == "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/dranswer_contract_test"
    )
    pytest_commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"]
    )
    assert "tests/test_backend_v13_postgres_boundary.py" in pytest_commands
    assert "test_agent_sqlite_to_postgres_migration.py" not in pytest_commands
    assert "test_system_sqlite_to_postgres_transform.py" not in pytest_commands
    assert "tests/test_agent_postgresql_core.py" in pytest_commands
    assert "tests/test_contract_test_server.py" in pytest_commands
    assert (
        "tests/test_agent_chat_feedback_v13.py" in pytest_commands
        or "python -m pytest -q -p no:cacheprovider tests" in pytest_commands
    )


def test_ci_runs_full_python_and_real_browser_regressions() -> None:
    workflow = load_yaml(
        PROJECT_ROOT / ".github" / "workflows" / "testbed-boundary-contract.yml"
    )
    steps = workflow["jobs"]["boundary-contract"]["steps"]
    commands = "\n".join(str(step.get("run", "")) for step in steps)

    assert "python -m pytest -q -p no:cacheprovider tests" in commands
    assert "npm ci" in commands
    assert "npm run test:browser" in commands
    assert "docker build -f Dockerfile.context-check -t da-drug-context-check ." in commands
    assert any(step.get("uses") == "actions/upload-artifact@v4" for step in steps)


def test_runtime_http_probe_covers_feedback_idempotency(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict, dict[str, str]]] = []

    def fake_post_json(
        url: str,
        payload: dict,
        *,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        calls.append((url, payload, headers or {}))
        if url.endswith("/api/ui/v1/chat/sync"):
            return 200, {
                "success": True,
                "data": {
                    "request_id": payload["request_id"],
                    "user_message_id": "user_msg_ui_runtime",
                    "assistant_message_id": "assistant_msg_ui_runtime",
                },
                "error": None,
            }
        if url.endswith("/api/ui/v1/chat/feedback"):
            if payload["opinion_text"].endswith("different body"):
                return 409, {
                    "success": False,
                    "data": None,
                    "error": {"code": "IDEMPOTENCY_CONFLICT"},
                }
            return 202, {
                "success": True,
                "data": {
                    "status": "accepted",
                    "request_id": payload["request_id"],
                    "assistant_message_id": payload[
                        "assistant_message_id"
                    ],
                    "reaction": payload.get("reaction"),
                    "opinion_submitted": bool(
                        payload.get("opinion_text")
                    ),
                    "opinion_submitted_at": (
                        "2026-07-26T00:00:00+00:00"
                    ),
                },
                "error": None,
            }
        if url.endswith("/agent/sync/chat"):
            return 401, {"error": {"code": "UNAUTHORIZED"}}
        if url.endswith("/agent/async/chat_feedback"):
            if payload.get("reaction") == "dislike":
                return 409, {"error": {"code": "IDEMPOTENCY_CONFLICT"}}
            return 202, {
                "status": "accepted",
                "reaction": payload.get("reaction"),
                "accepted_at": "2026-07-26T00:00:00+00:00",
            }
        raise AssertionError(f"unexpected_url:{url}")

    monkeypatch.setattr(
        boundary_contract,
        "_post_json",
        fake_post_json,
    )

    result = boundary_contract._probe_http_service_boundary(
        {
            "SYSTEM_BASE_URL": "http://system.test",
            "AGENT_BASE_URL": "http://agent.test",
            "AGENT_SYNC_API_TOKEN": "agent-sync-secret",
        }
    )

    assert result["feedback_status"] == 202
    assert result["feedback_idempotent_replay"] is True
    assert result["feedback_conflict_status"] == 409
    assert result["ui_feedback_status"] == 202
    assert result["ui_feedback_idempotent_replay"] is True
    assert result["ui_feedback_conflict_status"] == 409
    feedback_calls = [
        (payload, headers)
        for url, payload, headers in calls
        if url.endswith("/agent/async/chat_feedback")
    ]
    assert len(feedback_calls) == 3
    assert all(
        headers["Authorization"] == "Bearer agent-sync-secret"
        for _, headers in feedback_calls
    )
    assert all(
        payload["message_id"] == "assistant_msg_ui_runtime"
        for payload, _ in feedback_calls
    )
    assert all(
        payload["patient_id"] == "patient_0000000000000001"
        for payload, _ in feedback_calls
    )
    assert all("feedback" not in payload for payload, _ in feedback_calls)
    assert [payload["reaction"] for payload, _ in feedback_calls] == [
        "like",
        "like",
        "dislike",
    ]
    ui_chat_calls = [
        payload
        for url, payload, _headers in calls
        if url.endswith("/api/ui/v1/chat/sync")
    ]
    assert len(ui_chat_calls) == 2
    assert "patient_id" not in ui_chat_calls[0]
    assert "conversation_id" not in ui_chat_calls[0]
    ui_feedback_calls = [
        payload
        for url, payload, _headers in calls
        if url.endswith("/api/ui/v1/chat/feedback")
    ]
    assert len(ui_feedback_calls) == 3
    assert all(
        payload["assistant_message_id"] == "assistant_msg_ui_runtime"
        for payload in ui_feedback_calls
    )
    assert all(
        payload["reaction"] == "like"
        and "patient_id" not in payload
        and "conversation_id" not in payload
        for payload in ui_feedback_calls
    )
