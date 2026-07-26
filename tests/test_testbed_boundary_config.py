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


def test_committed_testbed_boundary_configuration_passes() -> None:
    result = verify_config(
        compose_path=PROJECT_ROOT / "docker-compose.yml",
        dev_compose_path=PROJECT_ROOT / "docker-compose.dev.yml",
        env_path=PROJECT_ROOT / ".env.9000.rule_based.example",
    )

    assert result["ok"] is True, result["violations"]


def test_compose_declares_separate_backend_agent_worker_and_phr_processes() -> None:
    compose = load_yaml(PROJECT_ROOT / "docker-compose.yml")
    services = compose["services"]

    assert "system_app.main:app" in services["system-app"]["command"]
    assert "agent_app.main:app" in services["agent-app"]["command"]
    assert services["agent-worker"]["command"] == "python -m agent_app.worker_main"
    assert "phr_app.main:app" in services["phr-app"]["command"]
    assert services["agent-app"]["environment"]["AGENT_EMBEDDED_WORKER_ENABLED"] == "false"
    assert services["agent-app"]["depends_on"]["system-app"]["condition"] == "service_healthy"
    assert "agent-app" not in services["system-app"]["depends_on"]
    assert services["agent-worker"]["depends_on"]["agent-app"]["condition"] == "service_healthy"
    assert "/health/ready" in " ".join(services["agent-app"]["healthcheck"]["test"])
    database_urls = {
        services["system-app"]["environment"]["SYSTEM_DATABASE_URL"],
        services["agent-app"]["environment"]["AGENT_DATABASE_URL"],
        services["phr-app"]["environment"]["PHR_DATABASE_URL"],
    }
    assert len(database_urls) == 3


def test_boundary_check_rejects_writable_backend_mount_for_agent() -> None:
    compose = deepcopy(load_yaml(PROJECT_ROOT / "docker-compose.yml"))
    backend_mount = next(
        mount
        for mount in compose["services"]["agent-app"]["volumes"]
        if mount["target"] == "/app/backend-read"
    )
    backend_mount["read_only"] = False

    violations = _compose_boundary_violations(compose)

    assert any("agent-app:/app/backend-read must mount system-runtime as ro" in item for item in violations)
    assert any("system-runtime writable services" in item for item in violations)


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


def test_boundary_check_rejects_non_read_only_uri_and_shared_tokens() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.rule_based.example")
    env["BACKEND_READ_DATABASE_URL"] = env["SYSTEM_DATABASE_URL"]
    env["BACKEND_API_TOKEN"] = env["AGENT_SYNC_API_TOKEN"]

    violations = _env_boundary_violations(env)

    assert any("mode=ro" in item for item in violations)
    assert any("uri=true" in item for item in violations)
    assert any("must be different" in item for item in violations)


def test_boundary_check_rejects_rule_based_provider_outside_testbed() -> None:
    env = read_env_file(PROJECT_ROOT / ".env.9000.rule_based.example")
    env["APP_ENV"] = "development"

    violations = _env_boundary_violations(env)

    assert any("rule-based LLM_PROVIDER requires APP_ENV" in item for item in violations)


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
    pytest_commands = "\n".join(
        str(step.get("run", "")) for step in job["steps"]
    )
    assert "tests/test_backend_v12_postgres_boundary.py" in pytest_commands
    assert (
        "tests/test_agent_chat_feedback_v12.py" in pytest_commands
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
        if url.endswith("/api/chat/sync"):
            return 200, {
                "request_id": payload["request_id"],
                "user_message_id": "user_msg_runtime",
                "assistant_message_id": "assistant_msg_runtime",
            }
        if url.endswith("/agent/sync/chat"):
            return 401, {"error": {"code": "UNAUTHORIZED"}}
        if url.endswith("/agent/async/chat_feedback"):
            if payload["feedback"] is False:
                return 409, {"error": {"code": "IDEMPOTENCY_CONFLICT"}}
            return 202, {"status": "accepted"}
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
        payload["message_id"] == "assistant_msg_runtime"
        for payload, _ in feedback_calls
    )
