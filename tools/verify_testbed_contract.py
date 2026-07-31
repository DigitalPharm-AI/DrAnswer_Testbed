from __future__ import annotations

import argparse
import difflib
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env.9000.example"
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.yml"
DEV_COMPOSE_FILE = PROJECT_ROOT / "docker-compose.dev.yml"
OPENAPI_FILE = PROJECT_ROOT / "docs" / "BACKEND_V13_WRITE_OPENAPI.json"
AGENT_CREDENTIAL_ENV_FILE = ".env.agent_app.secret"
BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"


def _new_request_id() -> str:
    return f"req_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class VolumeMount:
    source: str
    target: str
    read_only: bool
    mount_type: str


def env_file_paths(path: Path | str) -> tuple[Path, ...]:
    raw_parts = [
        part.strip()
        for part in str(path).replace(";", ",").split(",")
        if part.strip()
    ]
    paths: list[Path] = []
    for raw_part in raw_parts:
        candidate = Path(raw_part)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        paths.append(candidate.resolve())
    return tuple(paths)


def read_env_file(path: Path | str) -> dict[str, str]:
    values: dict[str, str] = {}
    for env_path in env_file_paths(path):
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            value = value.strip()
            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
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


def service_env_file_sources(service: dict) -> set[str]:
    sources: set[str] = set()
    for value in service.get("env_file") or []:
        if isinstance(value, dict):
            source = str(value.get("path") or "")
        else:
            source = str(value)
        if source:
            sources.add(_normalize_source(source))
    return sources


def verify_config(
    compose_path: Path = COMPOSE_FILE,
    dev_compose_path: Path = DEV_COMPOSE_FILE,
    env_path: Path | str = DEFAULT_ENV_FILE,
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
        "inputs": [
            str(compose_path),
            str(dev_compose_path),
            *(str(path) for path in env_file_paths(env_path)),
        ],
        "violations": violations,
    }


def verify_openapi(
    openapi_path: Path = OPENAPI_FILE,
    env_path: Path | str = DEFAULT_ENV_FILE,
) -> dict:
    contract_env = read_env_file(env_path)
    for key in (
        "AGENT_SYNC_API_TOKEN",
        "BACKEND_API_TOKEN",
        "BACKEND_READ_DATABASE_URL",
        "INTERNAL_API_TOKEN",
    ):
        if contract_env.get(key):
            os.environ[key] = contract_env[key]
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    from system_app.main import create_app
    from system_app.openapi_v13 import build_backend_v13_write_openapi

    expected = json.loads(openapi_path.read_text(encoding="utf-8"))
    actual = build_backend_v13_write_openapi(create_app())
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
                tofile="generated Backend v1.3 OpenAPI",
                lineterm="",
            )
        )[:120]
        violations.append(
            "Backend v1.3 OpenAPI drift detected; run "
            "`python tools/export_backend_v13_openapi.py` and review the contract change."
        )
    return {
        "ok": not violations,
        "check": "backend_v13_openapi_drift",
        "inputs": [str(openapi_path)],
        "violations": violations,
        "diff": diff,
    }


def verify_runtime(
    env_path: Path | str = DEFAULT_ENV_FILE,
) -> dict:
    env = read_env_file(env_path)
    return _verify_postgresql_runtime(env, env_path=env_path)


def _verify_postgresql_runtime(
    env: dict[str, str],
    *,
    env_path: Path | str,
) -> dict:
    from sqlalchemy import create_engine, text

    violations = _postgresql_env_violations(env)
    database_probes: dict[str, object] = {}
    for key in ("SYSTEM_DATABASE_URL", "AGENT_DATABASE_URL"):
        if violations:
            break
        engine = create_engine(
            env[key],
            pool_pre_ping=True,
            future=True,
        )
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1")).scalar_one()
                version_info = (
                    connection.dialect.server_version_info or ()
                )
                database_probes[key] = {
                    "read_ok": True,
                    "dialect": connection.dialect.name,
                    "server_version": (
                        ".".join(str(part) for part in version_info)
                        or "unknown"
                    ),
                }
        except Exception as exc:
            violations.append(
                f"{key} runtime probe failed: {type(exc).__name__}"
            )
        finally:
            engine.dispose()

    backend_probe: dict[str, object] = {
        "read_ok": False,
        "write_denied": False,
    }
    if not violations:
        try:
            backend_probe = _probe_postgresql_read_only(
                env["BACKEND_READ_DATABASE_URL"]
            )
        except Exception as exc:
            violations.append(
                "BACKEND_READ_DATABASE_URL runtime probe failed: "
                f"{type(exc).__name__}"
            )
    if backend_probe.get("read_ok") and not backend_probe.get(
        "write_denied"
    ):
        violations.append(
            "BACKEND_READ_DATABASE_URL role is not effectively read-only"
        )

    http_e2e: dict[str, object] = {"ok": False}
    if not violations:
        try:
            http_e2e = _probe_http_service_boundary(env)
        except (ValueError, OSError) as exc:
            violations.append(
                "Backend -> AI HTTP boundary probe failed: "
                f"{type(exc).__name__}"
            )
    return {
        "ok": not violations,
        "check": "postgresql_runtime_boundary",
        "inputs": [
            str(path)
            for path in env_file_paths(env_path)
        ],
        "database_probes": database_probes,
        "backend_probe": backend_probe,
        "http_e2e": http_e2e,
        "violations": violations,
    }


def _probe_postgresql_read_only(
    database_url: str,
) -> dict[str, object]:
    from sqlalchemy import create_engine, text

    from shared.backend_read_contract import (
        BACKEND_READ_VIEW_COLUMNS,
        BACKEND_READ_VIEW_DEFINITIONS,
    )

    engine = create_engine(
        database_url,
        pool_pre_ping=True,
        future=True,
    )
    try:
        with engine.connect() as connection:
            transaction_read_only = str(
                connection.execute(
                    text("SHOW transaction_read_only")
                ).scalar_one()
            ).lower() in {"on", "true", "1"}
            default_read_only = str(
                connection.execute(
                    text("SHOW default_transaction_read_only")
                ).scalar_one()
            ).lower() in {"on", "true", "1"}
            database_create = bool(
                connection.execute(
                    text(
                        "SELECT has_database_privilege("
                        "current_user, current_database(), 'CREATE')"
                    )
                ).scalar_one()
            )
            schema_create = bool(
                connection.execute(
                    text(
                        "SELECT has_schema_privilege("
                        "current_user, current_schema(), 'CREATE')"
                    )
                ).scalar_one()
            )
            write_privileges = []
            for view_name in BACKEND_READ_VIEW_COLUMNS:
                connection.execute(
                    text(f"SELECT * FROM {view_name} WHERE 1 = 0")
                )
                can_write = any(
                    bool(
                        connection.execute(
                            text(
                                "SELECT has_table_privilege("
                                "current_user, :view_name, :privilege)"
                            ),
                            {
                                "view_name": view_name,
                                "privilege": privilege,
                            },
                        ).scalar_one()
                    )
                    for privilege in (
                        "INSERT",
                        "UPDATE",
                        "DELETE",
                        "TRUNCATE",
                    )
                )
                if can_write:
                    write_privileges.append(view_name)
            source_read_privileges = []
            source_tables = {
                str(table_name)
                for definition in BACKEND_READ_VIEW_DEFINITIONS.values()
                for table_name in dict(definition["sources"])
            }
            for table_name in source_tables:
                if bool(
                    connection.execute(
                        text(
                            "SELECT has_table_privilege("
                            "current_user, :table_name, 'SELECT')"
                        ),
                        {"table_name": table_name},
                    ).scalar_one()
                ):
                    source_read_privileges.append(table_name)
            version_info = connection.dialect.server_version_info or ()
        return {
            "read_ok": True,
            "write_denied": (
                transaction_read_only
                and default_read_only
                and not database_create
                and not schema_create
                and not write_privileges
                and not source_read_privileges
            ),
            "transaction_read_only": transaction_read_only,
            "default_transaction_read_only": default_read_only,
            "database_create_denied": not database_create,
            "schema_create_denied": not schema_create,
            "view_write_denied": not write_privileges,
            "source_table_read_denied": not source_read_privileges,
            "dialect": "postgresql",
            "server_version": (
                ".".join(str(part) for part in version_info)
                or "unknown"
            ),
        }
    finally:
        engine.dispose()


def _compose_boundary_violations(compose: dict) -> list[str]:
    violations: list[str] = []
    services = compose.get("services") or {}
    required_services = {
        "system-migrate",
        "system-app",
        "agent-migrate",
        "agent-app",
        "agent-worker",
    }
    missing = sorted(required_services - set(services))
    if missing:
        return [f"docker-compose.yml is missing services: {', '.join(missing)}"]
    if "phr-app" in services:
        violations.append(
            "docker-compose.yml must not run the legacy phr-app in the active v1.3 stack"
        )

    volumes = compose.get("volumes") or {}
    required_volumes = {"system-runtime", "agent-runtime"}
    if missing_volumes := sorted(required_volumes - set(volumes)):
        violations.append(f"docker-compose.yml is missing runtime volumes: {', '.join(missing_volumes)}")
    if "phr-runtime" in volumes:
        violations.append(
            "docker-compose.yml must not declare the legacy phr-runtime volume"
        )

    database_keys = {
        "system-migrate": {
            "SYSTEM_DATABASE_URL",
            "SYSTEM_MIGRATION_DATABASE_URL",
        },
        "agent-migrate": {
            "AGENT_DATABASE_URL",
            "AGENT_MIGRATION_DATABASE_URL",
        },
        "agent-app": {
            "AGENT_DATABASE_URL",
            "BACKEND_READ_DATABASE_URL",
        },
        "agent-worker": {
            "AGENT_DATABASE_URL",
            "BACKEND_READ_DATABASE_URL",
        },
        "system-app": {"SYSTEM_DATABASE_URL"},
    }
    expected_env_files = {
        "system-migrate": {".env.system_app"},
        "agent-migrate": {".env.agent_app"},
        "agent-app": {".env.agent_app", AGENT_CREDENTIAL_ENV_FILE},
        "agent-worker": {".env.agent_app", AGENT_CREDENTIAL_ENV_FILE},
        "agent-langfuse-exporter": {".env.agent_app"},
        "system-app": {".env.system_app"},
    }
    for service_name, keys in database_keys.items():
        service_env = service_environment(services[service_name])
        for key in keys:
            if key in service_env:
                violations.append(
                    f"{service_name}.{key} must come from service env_file "
                    "so .env.agent_app/.env.system_app is not overwritten"
                )
    for service_name, expected in expected_env_files.items():
        if service_name not in services:
            continue
        actual_env_files = service_env_file_sources(
            services[service_name]
        )
        missing_env_files = expected - actual_env_files
        if missing_env_files:
            violations.append(
                f"{service_name} is missing env_file entries: "
                f"{', '.join(sorted(missing_env_files))}"
            )
        unexpected_env_files = actual_env_files - expected
        if unexpected_env_files:
            violations.append(
                f"{service_name} has unexpected env_file entries: "
                f"{', '.join(sorted(unexpected_env_files))}"
            )

    agent_runtime_services = {"agent-app", "agent-worker"}
    for service_name in agent_runtime_services:
        if BEDROCK_BEARER_ENV_KEY in service_environment(
            services[service_name]
        ):
            violations.append(
                f"{service_name}.{BEDROCK_BEARER_ENV_KEY} must come from "
                f"{AGENT_CREDENTIAL_ENV_FILE}, never inline environment"
            )
    for service_name in (
        "system-migrate",
        "agent-migrate",
        "agent-langfuse-exporter",
        "system-app",
    ):
        if service_name not in services:
            continue
        service_env = service_environment(services[service_name])
        if service_env.get(BEDROCK_BEARER_ENV_KEY) != "":
            violations.append(
                f"{service_name}.{BEDROCK_BEARER_ENV_KEY} must be explicitly "
                "overridden with an empty value"
            )

    expected_mounts = {
        "system-migrate": {},
        "agent-migrate": {},
        "system-app": {"/app/runtime/system": ("system-runtime", False)},
        "agent-app": {
            "/app/runtime/agent": ("agent-runtime", False),
        },
        "agent-worker": {
            "/app/runtime/agent": ("agent-runtime", False),
        },
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

    agent_dependencies = services["agent-app"].get("depends_on") or {}
    migration_dependency = agent_dependencies.get("agent-migrate") or {}
    if (
        migration_dependency.get("condition")
        != "service_completed_successfully"
    ):
        violations.append(
            "agent-app must wait for the dedicated agent-migrate step"
        )
    system_dependency = agent_dependencies.get("system-app") or {}
    if system_dependency.get("condition") != "service_healthy":
        violations.append("agent-app must wait for system-app health before verifying the Backend read contract")
    system_dependencies = services["system-app"].get("depends_on") or {}
    system_migration_dependency = (
        system_dependencies.get("system-migrate") or {}
    )
    if (
        system_migration_dependency.get("condition")
        != "service_completed_successfully"
    ):
        violations.append(
            "system-app must wait for the dedicated system-migrate step"
        )
    if "agent-app" in system_dependencies:
        violations.append("system-app must not depend on agent-app; that creates a Backend read-contract startup cycle")
    worker_dependencies = services["agent-worker"].get("depends_on") or {}
    if (worker_dependencies.get("agent-app") or {}).get("condition") != "service_healthy":
        violations.append("agent-worker must wait for agent-app health")
    agent_healthcheck = services["agent-app"].get("healthcheck") or {}
    agent_health_command = " ".join(str(part) for part in agent_healthcheck.get("test") or [])
    if "/health/ready" not in agent_health_command:
        violations.append("agent-app healthcheck must use the fail-closed /health/ready endpoint")
    for service_name in required_services:
        service_env = service_environment(services[service_name])
        for legacy_key in ("PHR_BASE_URL", "PHR_DATABASE_URL"):
            if legacy_key in service_env:
                violations.append(
                    f"{service_name} must not configure legacy {legacy_key}"
                )

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
        "agent-runtime": {
            "agent-app",
            "agent-worker",
        },
    }
    for source, expected in expected_writers.items():
        if writers[source] != expected:
            violations.append(f"{source} writable services must be {sorted(expected)}, got {sorted(writers[source])}")
    return violations


def _dev_compose_boundary_violations(compose: dict) -> list[str]:
    violations: list[str] = []
    services = compose.get("services") or {}
    if "phr-app" in services:
        violations.append(
            "docker-compose.dev.yml must not run the legacy phr-app"
        )
    application_sources = {"./system_app", "./agent_app"}
    allowed_application_sources = {
        "system-app": {"./system_app"},
        "agent-app": {"./agent_app"},
        "agent-worker": {"./agent_app"},
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


def _env_boundary_violations(
    env: dict[str, str],
) -> list[str]:
    violations: list[str] = []
    if BEDROCK_BEARER_ENV_KEY in env:
        violations.append(
            f"{BEDROCK_BEARER_ENV_KEY} must not be present in the common "
            "9000 contract profile"
        )
    violations.extend(_postgresql_env_violations(env))

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
        "deterministic_test",
        "deterministic-test",
    } and env.get("APP_ENV", "").strip().lower() not in {
        "test",
        "testing",
        "testbed",
    }:
        violations.append(
            "deterministic-test LLM_PROVIDER requires "
            "APP_ENV=test, testing, or testbed"
        )
    return violations


def _postgresql_env_violations(env: dict[str, str]) -> list[str]:
    violations: list[str] = []
    parsed_urls = {}
    for key in (
        "SYSTEM_DATABASE_URL",
        "SYSTEM_MIGRATION_DATABASE_URL",
        "AGENT_DATABASE_URL",
        "AGENT_MIGRATION_DATABASE_URL",
        "BACKEND_READ_DATABASE_URL",
    ):
        raw_url = env.get(key, "").strip()
        try:
            parsed = make_url(raw_url)
        except Exception:
            violations.append(f"{key} must be a valid PostgreSQL URL")
            continue
        if not parsed.drivername.startswith("postgresql"):
            violations.append(f"{key} must use PostgreSQL")
            continue
        parsed_urls[key] = parsed

    system_url = parsed_urls.get("SYSTEM_DATABASE_URL")
    system_migration_url = parsed_urls.get(
        "SYSTEM_MIGRATION_DATABASE_URL"
    )
    agent_url = parsed_urls.get("AGENT_DATABASE_URL")
    agent_migration_url = parsed_urls.get(
        "AGENT_MIGRATION_DATABASE_URL"
    )
    reader_url = parsed_urls.get("BACKEND_READ_DATABASE_URL")
    if (
        system_url is not None
        and agent_url is not None
        and system_url.database == agent_url.database
    ):
        violations.append(
            "SYSTEM_DATABASE_URL and AGENT_DATABASE_URL must use "
            "different databases"
        )
    if (
        system_url is not None
        and reader_url is not None
        and system_url.database != reader_url.database
    ):
        violations.append(
            "BACKEND_READ_DATABASE_URL must target the Backend database"
        )
    if (
        system_url is not None
        and reader_url is not None
        and system_url.username == reader_url.username
    ):
        violations.append(
            "BACKEND_READ_DATABASE_URL must use a dedicated reader role"
        )
    for runtime_key, migration_key, runtime_url, migration_url in (
        (
            "SYSTEM_DATABASE_URL",
            "SYSTEM_MIGRATION_DATABASE_URL",
            system_url,
            system_migration_url,
        ),
        (
            "AGENT_DATABASE_URL",
            "AGENT_MIGRATION_DATABASE_URL",
            agent_url,
            agent_migration_url,
        ),
    ):
        if runtime_url is None or migration_url is None:
            continue
        if runtime_url.database != migration_url.database:
            violations.append(
                f"{migration_key} must target the same database as "
                f"{runtime_key}"
            )
        if runtime_url.username == migration_url.username:
            violations.append(
                f"{migration_key} must use a role distinct from "
                f"{runtime_key}"
            )

    for key in (
        "SYSTEM_STARTUP_MIGRATIONS_ENABLED",
        "AGENT_STARTUP_MIGRATIONS_ENABLED",
    ):
        if env.get(key, "").strip().lower() != "false":
            violations.append(
                f"{key} must be false in every environment"
            )
    return violations


def _probe_http_service_boundary(env: dict[str, str]) -> dict[str, object]:
    system_base_url = env.get("SYSTEM_BASE_URL", "").rstrip("/")
    agent_base_url = env.get("AGENT_BASE_URL", "").rstrip("/")
    if not system_base_url or not agent_base_url:
        raise ValueError("SYSTEM_BASE_URL and AGENT_BASE_URL are required")

    request_id = _new_request_id()
    patient_id = (
        env.get("PATIENT_ID", "").strip()
        or "patient_0000000000000001"
    )
    message = "테스트베드 Backend와 AI 서비스 경계를 확인합니다."
    payload = {
        "request_id": request_id,
        "message": message,
        "requested_return_type": "text",
    }
    first_status, first = _post_json(
        f"{system_base_url}/api/ui/v1/chat/sync",
        payload,
    )
    if first_status != 200 or first.get("success") is not True:
        raise ValueError(
            "Backend /api/ui/v1/chat/sync returned "
            f"{first_status}: {first}"
        )
    second_status, second = _post_json(
        f"{system_base_url}/api/ui/v1/chat/sync",
        payload,
    )
    if second_status != 200 or second.get("success") is not True:
        raise ValueError(f"Backend idempotent replay returned {second_status}: {second}")
    if first != second:
        raise ValueError("Backend idempotent replay returned a different response")
    first_data = first.get("data")
    if not isinstance(first_data, dict):
        raise ValueError("Backend UI chat response data is missing")
    if first_data.get("request_id") != request_id:
        raise ValueError("Backend chat response request_id mismatch")
    user_message_id = str(first_data.get("user_message_id") or "")
    assistant_message_id = str(first_data.get("assistant_message_id") or "")
    if "conversation_id" in first_data:
        raise ValueError("Backend chat response must not expose conversation_id")
    if not user_message_id or not assistant_message_id or user_message_id == assistant_message_id:
        raise ValueError("Backend must persist distinct user and assistant message IDs")
    if "trace_id" in first_data:
        raise ValueError("Backend client response must not expose AI internal trace_id")

    unauthenticated_agent_payload = {
        "request_id": _new_request_id(),
        "message_id": user_message_id,
        "patient_id": patient_id,
        "requested_return_type": "text",
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
    feedback_request_id = _new_request_id()
    feedback_payload = {
        "request_id": feedback_request_id,
        "message_id": assistant_message_id,
        "patient_id": patient_id,
        "reaction": "like",
        "feedback_at": datetime.now(UTC).isoformat(),
    }
    feedback_headers = {"Authorization": f"Bearer {agent_sync_token}"}
    feedback_status, feedback_response = _post_json(
        f"{agent_base_url}/agent/async/chat_feedback",
        feedback_payload,
        headers=feedback_headers,
    )
    feedback_accepted_at = _aware_datetime_or_none(
        feedback_response.get("accepted_at")
    )
    if (
        feedback_status != 202
        or feedback_response.get("status") != "accepted"
        or feedback_response.get("reaction") != "like"
        or feedback_accepted_at is None
    ):
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
    feedback_conflict_payload["reaction"] = "dislike"
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

    ui_feedback_request_id = _new_request_id()
    ui_feedback_payload = {
        "request_id": ui_feedback_request_id,
        "assistant_message_id": assistant_message_id,
        "reaction": "like",
        "opinion_text": "runtime BFF opinion",
        "feedback_at": datetime.now(UTC).isoformat(),
    }
    ui_feedback_status, ui_feedback_response = _post_json(
        f"{system_base_url}/api/ui/v1/chat/feedback",
        ui_feedback_payload,
    )
    if (
        ui_feedback_status != 202
        or ui_feedback_response.get("success") is not True
        or not isinstance(ui_feedback_response.get("data"), dict)
        or ui_feedback_response["data"].get("reaction") != "like"
        or ui_feedback_response["data"].get("opinion_submitted")
        is not True
    ):
        raise ValueError(
            "Backend UI feedback acceptance mismatch: "
            f"status={ui_feedback_status} body={ui_feedback_response}"
        )
    ui_feedback_replay_status, ui_feedback_replay = _post_json(
        f"{system_base_url}/api/ui/v1/chat/feedback",
        ui_feedback_payload,
    )
    if (
        ui_feedback_replay_status != 202
        or ui_feedback_replay != ui_feedback_response
    ):
        raise ValueError(
            "Backend UI feedback replay mismatch: "
            f"status={ui_feedback_replay_status} body={ui_feedback_replay}"
        )
    ui_feedback_conflict_payload = dict(ui_feedback_payload)
    ui_feedback_conflict_payload["opinion_text"] = (
        "runtime BFF opinion with a different body"
    )
    ui_feedback_conflict_status, _ = _post_json(
        f"{system_base_url}/api/ui/v1/chat/feedback",
        ui_feedback_conflict_payload,
    )
    if ui_feedback_conflict_status != 409:
        raise ValueError(
            "Backend UI feedback request_id conflict returned "
            f"{ui_feedback_conflict_status}, expected 409"
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
        "ui_feedback_status": ui_feedback_status,
        "ui_feedback_idempotent_replay": (
            ui_feedback_replay == ui_feedback_response
        ),
        "ui_feedback_conflict_status": ui_feedback_conflict_status,
        "evidence": (
            "React /api/ui/v1/chat/sync -> Backend identity injection -> "
            "Agent /agent/sync/chat -> read-only Backend DB; "
            "Backend feedback -> Agent /agent/async/chat_feedback; "
            "React /api/ui/v1/chat/feedback -> Backend identity injection -> Agent"
        ),
    }


def _aware_datetime_or_none(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


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
        description="Verify Backend/AI storage boundaries and the committed Backend v1.3 OpenAPI contract."
    )
    parser.add_argument(
        "--scope",
        choices=("all", "config", "openapi", "runtime"),
        default="all",
        help="all checks config + OpenAPI; runtime is intended for a running local 9000 stack",
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="one env path or a comma/semicolon-separated precedence list",
    )
    parser.add_argument(
        "--runtime",
        action="store_true",
        help="also execute PostgreSQL connectivity and reader-role probes",
    )
    parser.add_argument("--json", action="store_true", help="emit only machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_path = args.env_file
    results: list[dict] = []
    if args.scope in {"all", "config"}:
        results.append(
            verify_config(
                env_path=env_path,
            )
        )
    if args.scope in {"all", "openapi"}:
        results.append(verify_openapi(env_path=env_path))
    if args.scope == "runtime" or args.runtime:
        results.append(
            verify_runtime(
                env_path=env_path,
            )
        )
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
