from __future__ import annotations

import argparse
import difflib
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.9000.rule_based.example"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
DEV_COMPOSE_FILE = PROJECT_ROOT / "docker-compose.dev.yml"
OPENAPI_FILE = PROJECT_ROOT / "docs" / "BACKEND_V12_WRITE_OPENAPI.json"


@dataclass(frozen=True)
class VolumeMount:
    source: str
    target: str
    read_only: bool
    mount_type: str


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a YAML object")
    return payload


def service_environment(service: dict) -> dict[str, str]:
    environment = service.get("environment") or {}
    if isinstance(environment, dict):
        return {str(key): "" if value is None else str(value) for key, value in environment.items()}
    values: dict[str, str] = {}
    for item in environment:
        key, separator, value = str(item).partition("=")
        values[key] = value if separator else ""
    return values


def service_mounts(service: dict) -> list[VolumeMount]:
    mounts: list[VolumeMount] = []
    for value in service.get("volumes") or []:
        if isinstance(value, dict):
            mounts.append(
                VolumeMount(
                    source=_normalize_source(str(value.get("source") or "")),
                    target=str(value.get("target") or ""),
                    read_only=bool(value.get("read_only", False)),
                    mount_type=str(value.get("type") or "volume"),
                )
            )
            continue
        parts = str(value).split(":")
        if len(parts) < 2:
            continue
        mounts.append(
            VolumeMount(
                source=_normalize_source(parts[0]),
                target=parts[1],
                read_only=any(part == "ro" for part in parts[2:]),
                mount_type="short",
            )
        )
    return mounts


def verify_config(
    compose_path: Path = COMPOSE_FILE,
    dev_compose_path: Path = DEV_COMPOSE_FILE,
    env_path: Path = DEFAULT_ENV_FILE,
) -> dict:
    compose = load_yaml(compose_path)
    dev_compose = load_yaml(dev_compose_path)
    env = read_env_file(env_path)
    violations = [
        *_compose_boundary_violations(compose),
        *_dev_compose_boundary_violations(dev_compose),
        *_env_boundary_violations(env),
    ]
    return {
        "ok": not violations,
        "check": "testbed_boundary_config",
        "inputs": [str(compose_path), str(dev_compose_path), str(env_path)],
        "violations": violations,
    }


def verify_openapi(
    openapi_path: Path = OPENAPI_FILE,
    env_path: Path = DEFAULT_ENV_FILE,
) -> dict:
    contract_env = read_env_file(env_path)
    for key in ("AGENT_SYNC_API_TOKEN", "BACKEND_API_TOKEN", "BACKEND_READ_DATABASE_URL"):
        if contract_env.get(key):
            os.environ[key] = contract_env[key]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from system_app.main import create_app
    from system_app.openapi_v12 import build_backend_v12_write_openapi

    expected = json.loads(openapi_path.read_text(encoding="utf-8"))
    actual = build_backend_v12_write_openapi(create_app())
    violations: list[str] = []
    diff: list[str] = []
    if actual != expected:
        expected_text = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        actual_text = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
        diff = list(
            difflib.unified_diff(
                expected_text,
                actual_text,
                fromfile=str(openapi_path),
                tofile="generated Backend v1.2 OpenAPI",
                lineterm="",
            )
        )[:120]
        violations.append(
            "Backend v1.2 OpenAPI drift detected; run "
            "`python tools/export_backend_v12_openapi.py` and review the contract change."
        )
    return {
        "ok": not violations,
        "check": "backend_v12_openapi_drift",
        "inputs": [str(openapi_path)],
        "violations": violations,
        "diff": diff,
    }


def verify_runtime(env_path: Path = DEFAULT_ENV_FILE) -> dict:
    env = read_env_file(env_path)
    violations: list[str] = []
    database_paths: dict[str, str] = {}
    for key in ("SYSTEM_DATABASE_URL", "AGENT_DATABASE_URL", "PHR_DATABASE_URL"):
        try:
            database_paths[key] = _sqlite_path(env.get(key, ""))
        except ValueError as exc:
            violations.append(f"{key}: {exc}")

    resolved_paths = {
        key: (PROJECT_ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
        for key, value in database_paths.items()
    }
    if len(set(resolved_paths.values())) != len(resolved_paths):
        violations.append("runtime databases must resolve to three distinct files")
    for key, path in resolved_paths.items():
        if not path.is_file():
            violations.append(f"{key} runtime database does not exist: {path}")

    backend_url = env.get("BACKEND_READ_DATABASE_URL", "")
    backend_probe: dict[str, object] = {"read_ok": False, "write_denied": False}
    http_e2e: dict[str, object] = {"ok": False}
    if not violations:
        try:
            backend_probe = _probe_sqlite_read_only(backend_url)
        except (ValueError, sqlite3.Error) as exc:
            violations.append(f"BACKEND_READ_DATABASE_URL runtime probe failed: {exc}")
    if backend_probe.get("read_ok") and not backend_probe.get("write_denied"):
        violations.append("BACKEND_READ_DATABASE_URL accepted a main-database write probe")
    if not violations:
        try:
            http_e2e = _probe_http_service_boundary(env)
        except (ValueError, OSError) as exc:
            violations.append(f"Backend -> AI HTTP boundary probe failed: {exc}")

    return {
        "ok": not violations,
        "check": "sqlite_runtime_boundary",
        "inputs": [str(env_path)],
        "database_paths": {key: str(value) for key, value in resolved_paths.items()},
        "backend_probe": backend_probe,
        "http_e2e": http_e2e,
        "violations": violations,
    }


def _compose_boundary_violations(compose: dict) -> list[str]:
    violations: list[str] = []
    services = compose.get("services") or {}
    required_services = {"system-app", "agent-app", "agent-worker", "phr-app"}
    missing = sorted(required_services - set(services))
    if missing:
        return [f"docker-compose.yml is missing services: {', '.join(missing)}"]

    volumes = compose.get("volumes") or {}
    required_volumes = {"system-runtime", "agent-runtime", "phr-runtime"}
    if missing_volumes := sorted(required_volumes - set(volumes)):
        violations.append(f"docker-compose.yml is missing runtime volumes: {', '.join(missing_volumes)}")

    expected_urls = {
        ("system-app", "SYSTEM_DATABASE_URL"): "sqlite:////app/runtime/system/system.db",
        ("agent-app", "AGENT_DATABASE_URL"): "sqlite:////app/runtime/agent/agent.db",
        ("agent-worker", "AGENT_DATABASE_URL"): "sqlite:////app/runtime/agent/agent.db",
        ("phr-app", "PHR_DATABASE_URL"): "sqlite:////app/runtime/phr/phr.db",
    }
    for (service_name, key), expected in expected_urls.items():
        actual = service_environment(services[service_name]).get(key)
        if actual != expected:
            violations.append(f"{service_name}.{key} must be {expected!r}, got {actual!r}")

    expected_mounts = {
        "system-app": {"/app/runtime/system": ("system-runtime", False)},
        "agent-app": {
            "/app/runtime/agent": ("agent-runtime", False),
            "/app/backend-read": ("system-runtime", True),
        },
        "agent-worker": {
            "/app/runtime/agent": ("agent-runtime", False),
            "/app/backend-read": ("system-runtime", True),
        },
        "phr-app": {"/app/runtime/phr": ("phr-runtime", False)},
    }
    for service_name, target_expectations in expected_mounts.items():
        mount_by_target = {mount.target: mount for mount in service_mounts(services[service_name])}
        for target, (source, read_only) in target_expectations.items():
            actual = mount_by_target.get(target)
            if actual is None:
                violations.append(f"{service_name} must mount {source} at {target}")
            elif actual.source != source or actual.read_only != read_only:
                mode = "ro" if read_only else "rw"
                violations.append(f"{service_name}:{target} must mount {source} as {mode}")

    reader_urls = {
        name: service_environment(services[name]).get("BACKEND_READ_DATABASE_URL", "")
        for name in ("agent-app", "agent-worker")
    }
    for service_name, database_url in reader_urls.items():
        violations.extend(_read_only_url_violations(service_name, database_url, "/app/backend-read/system.db"))

    agent_dependencies = services["agent-app"].get("depends_on") or {}
    system_dependency = agent_dependencies.get("system-app") or {}
    if system_dependency.get("condition") != "service_healthy":
        violations.append("agent-app must wait for system-app health before verifying the Backend read contract")
    system_dependencies = services["system-app"].get("depends_on") or {}
    if "agent-app" in system_dependencies:
        violations.append("system-app must not depend on agent-app; that creates a Backend read-contract startup cycle")
    worker_dependencies = services["agent-worker"].get("depends_on") or {}
    if (worker_dependencies.get("agent-app") or {}).get("condition") != "service_healthy":
        violations.append("agent-worker must wait for agent-app health")
    agent_healthcheck = services["agent-app"].get("healthcheck") or {}
    agent_health_command = " ".join(str(part) for part in agent_healthcheck.get("test") or [])
    if "/health/ready" not in agent_health_command:
        violations.append("agent-app healthcheck must use the fail-closed /health/ready endpoint")

    writers: dict[str, set[str]] = {name: set() for name in required_volumes}
    for service_name, service in services.items():
        for mount in service_mounts(service):
            if mount.source in writers and not mount.read_only:
                writers[mount.source].add(service_name)
            if mount.source == "data" or mount.source == "./data":
                if not mount.read_only and service_name != "system-app":
                    violations.append(f"{service_name} must not receive the Backend ./data bind mount as writable")
    expected_writers = {
        "system-runtime": {"system-app"},
        "agent-runtime": {"agent-app", "agent-worker"},
        "phr-runtime": {"phr-app"},
    }
    for source, expected in expected_writers.items():
        if writers[source] != expected:
            violations.append(f"{source} writable services must be {sorted(expected)}, got {sorted(writers[source])}")
    return violations


def _dev_compose_boundary_violations(compose: dict) -> list[str]:
    violations: list[str] = []
    services = compose.get("services") or {}
    application_sources = {"./system_app", "./agent_app", "./phr_app"}
    allowed_application_sources = {
        "system-app": {"./system_app"},
        "agent-app": {"./agent_app"},
        "agent-worker": {"./agent_app"},
        "phr-app": {"./phr_app"},
    }
    for service_name, service in services.items():
        mounts = service_mounts(service)
        if not mounts:
            violations.append(f"docker-compose.dev.yml {service_name} must declare read-only source mounts")
            continue
        for mount in mounts:
            if mount.source in {".", "./"} and mount.target == "/app":
                violations.append(f"docker-compose.dev.yml {service_name} must not mount the repository root at /app")
            if not mount.read_only:
                violations.append(f"docker-compose.dev.yml {service_name}:{mount.target} source mount must be read-only")
            if (
                mount.source in application_sources
                and mount.source not in allowed_application_sources.get(service_name, set())
            ):
                violations.append(
                    f"docker-compose.dev.yml {service_name} must not mount "
                    f"another service application source {mount.source}"
                )
    return violations


def _env_boundary_violations(env: dict[str, str]) -> list[str]:
    violations: list[str] = []
    expected_paths = {
        "SYSTEM_DATABASE_URL": "./runtime/system/system.db",
        "AGENT_DATABASE_URL": "./runtime/agent/agent.db",
        "PHR_DATABASE_URL": "./runtime/phr/phr.db",
    }
    for key, expected in expected_paths.items():
        try:
            actual = _sqlite_path(env.get(key, ""))
        except ValueError as exc:
            violations.append(f"{key}: {exc}")
            continue
        if _normalize_source(actual) != _normalize_source(expected):
            violations.append(f"{key} must resolve to {expected!r}, got {actual!r}")

    reader_url = env.get("BACKEND_READ_DATABASE_URL", "")
    violations.extend(_read_only_url_violations("9000 profile", reader_url, "./runtime/system/system.db"))

    agent_sync_token = env.get("AGENT_SYNC_API_TOKEN", "").strip()
    backend_api_token = env.get("BACKEND_API_TOKEN", "").strip()
    if not agent_sync_token:
        violations.append("AGENT_SYNC_API_TOKEN must be non-empty in the 9000 contract profile")
    if not backend_api_token:
        violations.append("BACKEND_API_TOKEN must be non-empty in the 9000 contract profile")
    if agent_sync_token and agent_sync_token == backend_api_token:
        violations.append("AGENT_SYNC_API_TOKEN and BACKEND_API_TOKEN must be different")

    if env.get("AGENT_EMBEDDED_WORKER_ENABLED", "").strip().lower() != "false":
        violations.append("AGENT_EMBEDDED_WORKER_ENABLED must be false")
    if env.get("LLM_PROVIDER", "").strip().lower() in {
        "rule_based",
        "rule-based",
        "local",
        "heuristic",
    } and env.get("APP_ENV", "").strip().lower() not in {
        "test",
        "testing",
        "testbed",
    }:
        violations.append(
            "rule-based LLM_PROVIDER requires APP_ENV=test, testing, or testbed"
        )
    return violations


def _read_only_url_violations(label: str, database_url: str, expected_path: str) -> list[str]:
    violations: list[str] = []
    try:
        actual_path = _sqlite_path(database_url)
    except ValueError as exc:
        return [f"{label} BACKEND_READ_DATABASE_URL: {exc}"]
    if _normalize_source(actual_path) != _normalize_source(expected_path):
        violations.append(f"{label} BACKEND_READ_DATABASE_URL must point to {expected_path!r}, got {actual_path!r}")
    query = _sqlite_query(database_url)
    if query.get("mode") != ["ro"]:
        violations.append(f"{label} BACKEND_READ_DATABASE_URL must include mode=ro")
    if [value.lower() for value in query.get("uri", [])] != ["true"]:
        violations.append(f"{label} BACKEND_READ_DATABASE_URL must include uri=true")
    return violations


def _sqlite_path(database_url: str) -> str:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("must be a sqlite:/// URL")
    database = database_url.split("?", 1)[0][len(prefix) :]
    if database.startswith("file:"):
        database = database[len("file:") :]
    if not database:
        raise ValueError("database path is empty")
    return database


def _sqlite_query(database_url: str) -> dict[str, list[str]]:
    _, separator, query = database_url.partition("?")
    return parse_qs(query, keep_blank_values=True) if separator else {}


def _probe_sqlite_read_only(database_url: str) -> dict[str, object]:
    database = _sqlite_path(database_url)
    query = _sqlite_query(database_url)
    sqlite_uri = f"file:{database}"
    sqlite_query = {key: values[-1] for key, values in query.items() if key != "uri" and values}
    if sqlite_query:
        sqlite_uri = f"{sqlite_uri}?{urlencode(sqlite_query)}"

    connection = sqlite3.connect(sqlite_uri, uri=True, timeout=3)
    try:
        table_count = int(connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0])
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        write_denied = False
        try:
            connection.execute("BEGIN")
            connection.execute(f"PRAGMA user_version = {user_version}")
        except sqlite3.OperationalError as exc:
            write_denied = "readonly" in str(exc).lower() or "read-only" in str(exc).lower()
        finally:
            connection.rollback()
        return {
            "read_ok": True,
            "write_denied": write_denied,
            "sqlite_master_rows": table_count,
        }
    finally:
        connection.close()


def _probe_http_service_boundary(env: dict[str, str]) -> dict[str, object]:
    system_base_url = env.get("SYSTEM_BASE_URL", "").rstrip("/")
    agent_base_url = env.get("AGENT_BASE_URL", "").rstrip("/")
    if not system_base_url or not agent_base_url:
        raise ValueError("SYSTEM_BASE_URL and AGENT_BASE_URL are required")

    suffix = uuid.uuid4().hex
    request_id = f"boundary-e2e-{suffix}"
    conversation_id = f"boundary-e2e-conversation-{suffix}"
    patient_id = "boundary-e2e-patient"
    message = "테스트베드 Backend와 AI 서비스 경계를 확인합니다."
    payload = {
        "request_id": request_id,
        "conversation_id": conversation_id,
        "patient_id": patient_id,
        "message": message,
        "requested_return_type": None,
    }
    first_status, first = _post_json(f"{system_base_url}/api/chat/sync", payload)
    if first_status != 200:
        raise ValueError(f"Backend /api/chat/sync returned {first_status}: {first}")
    second_status, second = _post_json(f"{system_base_url}/api/chat/sync", payload)
    if second_status != 200:
        raise ValueError(f"Backend idempotent replay returned {second_status}: {second}")
    if first != second:
        raise ValueError("Backend idempotent replay returned a different response")
    if first.get("request_id") != request_id:
        raise ValueError("Backend chat response request_id mismatch")
    user_message_id = str(first.get("user_message_id") or "")
    assistant_message_id = str(first.get("assistant_message_id") or "")
    if not user_message_id or not assistant_message_id or user_message_id == assistant_message_id:
        raise ValueError("Backend must persist distinct user and assistant message IDs")
    if "trace_id" in first:
        raise ValueError("Backend client response must not expose AI internal trace_id")

    unauthenticated_agent_payload = {
        "request_id": f"unauthenticated-{request_id}",
        "message_id": user_message_id,
        "conversation_id": conversation_id,
        "patient_id": patient_id,
        "requested_return_type": None,
        "message": message,
        "message_at": datetime.now(UTC).isoformat(),
    }
    unauthorized_status, _ = _post_json(
        f"{agent_base_url}/agent/sync/chat",
        unauthenticated_agent_payload,
    )
    if unauthorized_status != 401:
        raise ValueError(f"Agent direct chat without Bearer token returned {unauthorized_status}, expected 401")

    agent_sync_token = env.get("AGENT_SYNC_API_TOKEN", "").strip()
    if not agent_sync_token:
        raise ValueError("AGENT_SYNC_API_TOKEN is required for the feedback boundary probe")
    feedback_request_id = f"boundary-feedback-{suffix}"
    feedback_payload = {
        "request_id": feedback_request_id,
        "message_id": assistant_message_id,
        "conversation_id": conversation_id,
        "patient_id": patient_id,
        "feedback": True,
        "feedback_text": "runtime boundary feedback",
        "feedback_at": datetime.now(UTC).isoformat(),
    }
    feedback_headers = {"Authorization": f"Bearer {agent_sync_token}"}
    feedback_status, feedback_response = _post_json(
        f"{agent_base_url}/agent/async/chat_feedback",
        feedback_payload,
        headers=feedback_headers,
    )
    if feedback_status != 202 or feedback_response != {"status": "accepted"}:
        raise ValueError(
            "Agent feedback acceptance contract mismatch: "
            f"status={feedback_status} body={feedback_response}"
        )
    feedback_replay_status, feedback_replay_response = _post_json(
        f"{agent_base_url}/agent/async/chat_feedback",
        feedback_payload,
        headers=feedback_headers,
    )
    if (
        feedback_replay_status != 202
        or feedback_replay_response != feedback_response
    ):
        raise ValueError(
            "Agent feedback idempotent replay contract mismatch: "
            f"status={feedback_replay_status} body={feedback_replay_response}"
        )
    feedback_conflict_payload = dict(feedback_payload)
    feedback_conflict_payload["feedback"] = False
    feedback_conflict_status, _ = _post_json(
        f"{agent_base_url}/agent/async/chat_feedback",
        feedback_conflict_payload,
        headers=feedback_headers,
    )
    if feedback_conflict_status != 409:
        raise ValueError(
            "Agent feedback request_id conflict returned "
            f"{feedback_conflict_status}, expected 409"
        )

    return {
        "ok": True,
        "backend_status": first_status,
        "idempotent_replay": first == second,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "agent_unauthenticated_status": unauthorized_status,
        "feedback_status": feedback_status,
        "feedback_idempotent_replay": feedback_replay_response == feedback_response,
        "feedback_conflict_status": feedback_conflict_status,
        "evidence": (
            "Backend /api/chat/sync -> Agent /agent/sync/chat -> read-only Backend DB; "
            "Backend feedback -> Agent /agent/async/chat_feedback"
        ),
    }


def _post_json(
    url: str,
    payload: dict,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict]:
    request_headers = {"Content-Type": "application/json"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = int(response.status)
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{url} returned non-JSON status={status}") from exc
    if not isinstance(body, dict):
        raise ValueError(f"{url} returned a non-object JSON response")
    return status, body


def _normalize_source(value: str) -> str:
    return value.strip().replace("\\", "/").rstrip("/")


def _combined_result(results: list[dict]) -> dict:
    return {
        "ok": all(result["ok"] for result in results),
        "checks": results,
        "violations": [
            violation
            for result in results
            for violation in result.get("violations", [])
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify Backend/AI/PHR storage boundaries and the committed Backend v1.2 OpenAPI contract."
    )
    parser.add_argument(
        "--scope",
        choices=("all", "config", "openapi", "runtime"),
        default="all",
        help="all checks config + OpenAPI; runtime is intended for a running local 9000 stack",
    )
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--runtime", action="store_true", help="also execute the SQLite read/write-denial probe")
    parser.add_argument("--json", action="store_true", help="emit only machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = args.env_file if args.env_file.is_absolute() else PROJECT_ROOT / args.env_file
    results: list[dict] = []
    if args.scope in {"all", "config"}:
        results.append(verify_config(env_path=env_path))
    if args.scope in {"all", "openapi"}:
        results.append(verify_openapi(env_path=env_path))
    if args.scope == "runtime" or args.runtime:
        results.append(verify_runtime(env_path=env_path))
    result = _combined_result(results)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["ok"] else "FAIL"
        print(f"{status}: testbed boundary/contract verification")
        for check in results:
            print(f"- {'PASS' if check['ok'] else 'FAIL'} {check['check']}")
        for violation in result["violations"]:
            print(f"  - {violation}")
        for check in results:
            for line in check.get("diff", []):
                print(line)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
