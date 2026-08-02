from __future__ import annotations

import atexit
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.backend_read_contract import BACKEND_READ_VIEW_COLUMNS  # noqa: E402

BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"


def _sanitized_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(BEDROCK_BEARER_ENV_KEY, None)
    return environment


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return int(server_socket.getsockname()[1])


def _url_with_search_path(url: URL, schema_name: str) -> str:
    query = dict(url.query)
    query["options"] = f"-csearch_path={schema_name}"
    return url.set(query=query).render_as_string(hide_password=False)


def _reader_url_with_search_path(url: URL, schema_name: str) -> str:
    query = dict(url.query)
    query["options"] = (
        f"-csearch_path={schema_name} "
        "-cdefault_transaction_read_only=on"
    )
    return url.set(query=query).render_as_string(hide_password=False)


def _wait_for_json(
    url: str,
    *,
    process: subprocess.Popen[str] | None = None,
    headers: dict[str, str] | None = None,
    predicate=lambda payload: True,
    timeout_seconds: float = 90.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(
                f"process exited before becoming ready: {url}"
            )
        try:
            request = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(request, timeout=2.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
                if isinstance(payload, dict) and predicate(payload):
                    return payload
        except (
            json.JSONDecodeError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            TimeoutError,
        ) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise TimeoutError(f"service did not become ready: {url}: {last_error}")


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10.0) as response:
        body = json.loads(response.read().decode("utf-8"))
        if response.status != 200 or not isinstance(body, dict):
            raise RuntimeError(
                f"browser test setup request failed: {url}: "
                f"{response.status}"
            )
        return body


def _start_process(
    command: list[str],
    *,
    environment: dict[str, str],
    log_path: Path,
) -> tuple[subprocess.Popen[str], Any]:
    log_handle = log_path.open("w", encoding="utf-8")
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        ),
    )
    return process, log_handle


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            process.wait(timeout=8)
            return
        except subprocess.TimeoutExpired:
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
    else:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_migration(
    module_name: str,
    *,
    service_name: str,
    environment: dict[str, str],
) -> None:
    migration_environment = {
        **environment,
        "DA_DRUG_SERVICE": service_name,
    }
    completed = subprocess.run(
        [sys.executable, "-m", module_name],
        cwd=ROOT,
        env=migration_environment,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{module_name} failed before browser test")


def _grant_backend_reader(
    admin_engine,
    *,
    schema_name: str,
    role_name: str,
) -> None:
    with admin_engine.begin() as connection:
        quoted_role = connection.dialect.identifier_preparer.quote(
            role_name
        )
        connection.execute(
            text(f'REVOKE CREATE ON SCHEMA "{schema_name}" FROM PUBLIC')
        )
        connection.execute(
            text(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA "
                f'"{schema_name}" FROM PUBLIC'
            )
        )
        connection.execute(
            text(
                "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "
                f'"{schema_name}" FROM PUBLIC'
            )
        )
        connection.execute(
            text(
                f'GRANT USAGE ON SCHEMA "{schema_name}" '
                f"TO {quoted_role}"
            )
        )
        qualified_views = ", ".join(
            f'"{schema_name}"."{view_name}"'
            for view_name in BACKEND_READ_VIEW_COLUMNS
        )
        connection.execute(
            text(
                f"GRANT SELECT ON TABLE {qualified_views} "
                f"TO {quoted_role}"
            )
        )


def _boundary_evidence(agent_url: str, system_url: str) -> dict[str, Any]:
    agent_engine = create_engine(agent_url, future=True)
    system_engine = create_engine(system_url, future=True)
    try:
        with agent_engine.connect() as connection:
            sync_completed = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_sync_requests "
                        "WHERE api_path = '/agent/sync/chat' "
                        "AND status = 'COMPLETED' "
                        "AND completed_at IS NOT NULL"
                    )
                ).scalar_one()
            )
            feedback_accepted = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_feedback_links "
                        "WHERE api_path = '/agent/async/chat_feedback'"
                    )
                ).scalar_one()
            )
            missed_tasks = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_async_tasks "
                        "WHERE task_type = 'missed_dose' "
                        "AND status = 'done' "
                        "AND completed_at IS NOT NULL"
                    )
                ).scalar_one()
            )
        with system_engine.connect() as connection:
            completed_jobs = int(
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM agent_jobs "
                        "WHERE job_type = 'missed_dose' AND status = 'done'"
                    )
                ).scalar_one()
            )
    finally:
        agent_engine.dispose()
        system_engine.dispose()
    evidence = {
        "agent_sync_chat_completed": sync_completed,
        "agent_feedback_requests_accepted": feedback_accepted,
        "agent_missed_dose_callbacks_sent": missed_tasks,
        "backend_missed_dose_jobs_done": completed_jobs,
    }
    if not all(value > 0 for value in evidence.values()):
        raise RuntimeError(
            "real service boundary evidence was incomplete: "
            f"{json.dumps(evidence, ensure_ascii=False)}"
        )
    return evidence


def _stream_regression_evidence(
    agent_url: str,
    system_url: str,
    summary_path: Path,
) -> dict[str, Any]:
    report = json.loads(summary_path.read_text(encoding="utf-8"))
    cases = {
        "abort_replay": {
            "request_id": report["abort_replay"]["request_id"],
            "assistant_count": 1,
            "agent_status": "COMPLETED",
            "user_status": "completed",
            "trace_rows": {1},
            "model_calls": 1,
            "attempt_epoch": 1,
        },
        "concurrent_same_request": {
            "request_id": report["concurrent_same_request"][
                "request_id"
            ],
            "assistant_count": 1,
            "agent_status": "COMPLETED",
            "user_status": "completed",
            "trace_rows": {1, 2},
            "model_calls": 1,
            "attempt_epoch": 1,
        },
        "failure_after_token": {
            "request_id": report["failure_after_token"]["request_id"],
            "assistant_count": 1,
            "agent_status": "COMPLETED",
            "user_status": "completed",
            "trace_rows": {2},
            "model_calls": 2,
            "attempt_epoch": 2,
        },
        "slow_consumer": {
            "request_id": report["slow_consumer"]["request_id"],
            "assistant_count": 1,
            "agent_status": "COMPLETED",
            "user_status": "completed",
            "trace_rows": {1},
            "model_calls": 1,
            "attempt_epoch": 1,
        },
    }
    agent_engine = create_engine(agent_url, future=True)
    system_engine = create_engine(system_url, future=True)
    evidence: dict[str, Any] = {}
    try:
        for case_name, expectation in cases.items():
            request_id = str(expectation["request_id"])
            with system_engine.connect() as connection:
                message_counts = {
                    str(row.role): int(row.count)
                    for row in connection.execute(
                        text(
                            "SELECT role, COUNT(*) AS count "
                            "FROM chat_messages "
                            "WHERE ai_request_id = :request_id "
                            "GROUP BY role"
                        ),
                        {"request_id": request_id},
                    )
                }
                user_status = connection.execute(
                    text(
                        "SELECT processing_status FROM chat_messages "
                        "WHERE ai_request_id = :request_id "
                        "AND role = 'user'"
                    ),
                    {"request_id": request_id},
                ).scalar_one_or_none()
            with agent_engine.connect() as connection:
                sync_rows = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM agent_sync_requests "
                            "WHERE api_path = '/agent/sync/chat' "
                            "AND request_id = :request_id"
                        ),
                        {"request_id": request_id},
                    ).scalar_one()
                )
                sync_status = connection.execute(
                    text(
                        "SELECT status FROM agent_sync_requests "
                        "WHERE api_path = '/agent/sync/chat' "
                        "AND request_id = :request_id"
                    ),
                    {"request_id": request_id},
                ).scalar_one_or_none()
                attempt_epoch = connection.execute(
                    text(
                        "SELECT attempt_epoch FROM agent_sync_requests "
                        "WHERE api_path = '/agent/sync/chat' "
                        "AND request_id = :request_id"
                    ),
                    {"request_id": request_id},
                ).scalar_one_or_none()
                trace_rows = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM agent_run_traces "
                            "WHERE request_id = :request_id"
                        ),
                        {"request_id": request_id},
                    ).scalar_one()
                )
                trace_status_counts = {
                    str(row.status): int(row.count)
                    for row in connection.execute(
                        text(
                            "SELECT status, COUNT(*) AS count "
                            "FROM agent_run_traces "
                            "WHERE request_id = :request_id "
                            "GROUP BY status"
                        ),
                        {"request_id": request_id},
                    )
                }
                model_steps = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(*) FROM agent_run_steps s "
                            "JOIN agent_run_traces t "
                            "ON t.trace_id = s.trace_id "
                            "WHERE t.request_id = :request_id "
                            "AND s.step_type = 'model_call'"
                        ),
                        {"request_id": request_id},
                    ).scalar_one()
                )
                model_trace_rows = int(
                    connection.execute(
                        text(
                            "SELECT COUNT(DISTINCT s.trace_id) "
                            "FROM agent_run_steps s "
                            "JOIN agent_run_traces t "
                            "ON t.trace_id = s.trace_id "
                            "WHERE t.request_id = :request_id "
                            "AND s.step_type = 'model_call'"
                        ),
                        {"request_id": request_id},
                    ).scalar_one()
                )
            case_evidence = {
                "request_id": request_id,
                "backend_user_messages": message_counts.get("user", 0),
                "backend_assistant_messages": message_counts.get(
                    "assistant", 0
                ),
                "backend_user_status": user_status,
                "agent_sync_rows": sync_rows,
                "agent_sync_status": sync_status,
                "agent_attempt_epoch": attempt_epoch,
                "agent_trace_rows": trace_rows,
                "agent_trace_status_counts": trace_status_counts,
                "agent_model_call_steps": model_steps,
                "agent_model_execution_traces": model_trace_rows,
            }
            evidence[case_name] = case_evidence
            valid = (
                case_evidence["backend_user_messages"] == 1
                and case_evidence["backend_assistant_messages"]
                == expectation["assistant_count"]
                and case_evidence["backend_user_status"]
                == expectation["user_status"]
                and case_evidence["agent_sync_rows"] == 1
                and case_evidence["agent_sync_status"]
                == expectation["agent_status"]
                and case_evidence["agent_attempt_epoch"]
                == expectation["attempt_epoch"]
                and case_evidence["agent_trace_rows"]
                in expectation["trace_rows"]
                and case_evidence["agent_model_call_steps"]
                == expectation["model_calls"]
                and case_evidence["agent_model_execution_traces"]
                == expectation["model_calls"]
            )
            if not valid:
                raise RuntimeError(
                    "stream regression persistence evidence mismatch: "
                    f"{case_name}: "
                    f"{json.dumps(case_evidence, ensure_ascii=False)}"
                )
    finally:
        agent_engine.dispose()
        system_engine.dispose()
    return evidence


def main() -> int:
    postgres_url = (
        os.getenv("BROWSER_POSTGRES_TEST_DATABASE_URL", "").strip()
        or os.getenv(
            "BACKEND_POSTGRES_BOUNDARY_TEST_DATABASE_URL",
            "",
        ).strip()
    )
    parsed_url = make_url(postgres_url)
    if not parsed_url.drivername.startswith("postgresql"):
        raise RuntimeError(
            "BROWSER_POSTGRES_TEST_DATABASE_URL must use PostgreSQL"
        )

    unique_suffix = uuid.uuid4().hex[:16]
    system_schema = f"browser_v13_system_{unique_suffix}"
    agent_schema = f"browser_v13_agent_{unique_suffix}"
    configured_reader_value = (
        os.getenv(
            "BROWSER_POSTGRES_READER_DATABASE_URL",
            "",
        ).strip()
        or os.getenv("BACKEND_READ_DATABASE_URL", "").strip()
    )
    configured_reader_url = (
        make_url(configured_reader_value)
        if configured_reader_value
        else None
    )
    if configured_reader_url is not None:
        if (
            not configured_reader_url.drivername.startswith("postgresql")
            or configured_reader_url.database != parsed_url.database
            or configured_reader_url.host != parsed_url.host
            or configured_reader_url.port != parsed_url.port
            or not configured_reader_url.username
        ):
            raise RuntimeError(
                "BROWSER_POSTGRES_READER_DATABASE_URL must point to "
                "the same PostgreSQL database as the browser admin URL"
            )
        reader_role = configured_reader_url.username
        reader_password = ""
        reader_role_created = False
    else:
        reader_role = f"browser_v13_reader_{unique_suffix}"
        reader_password = uuid.uuid4().hex
        reader_role_created = True
    admin_engine = create_engine(parsed_url, future=True)
    database_name = str(parsed_url.database or "")
    quoted_database = admin_engine.dialect.identifier_preparer.quote(
        database_name
    )
    objects_created = False
    cleanup_complete = False

    def cleanup_database_objects() -> None:
        nonlocal cleanup_complete
        if cleanup_complete:
            return
        cleanup_complete = True
        try:
            if objects_created:
                with admin_engine.begin() as connection:
                    quoted_reader_role = (
                        connection.dialect.identifier_preparer.quote(
                            reader_role
                        )
                    )
                    if reader_role_created:
                        connection.execute(
                            text(f"DROP OWNED BY {quoted_reader_role}")
                        )
                    connection.execute(
                        text(
                            f'DROP SCHEMA IF EXISTS "{agent_schema}" CASCADE'
                        )
                    )
                    connection.execute(
                        text(
                            f'DROP SCHEMA IF EXISTS "{system_schema}" CASCADE'
                        )
                    )
                    if reader_role_created:
                        connection.execute(
                            text(
                                f"REVOKE ALL PRIVILEGES ON DATABASE "
                                f"{quoted_database} FROM "
                                f"{quoted_reader_role}"
                            )
                        )
                        connection.execute(
                            text(
                                f"DROP ROLE IF EXISTS "
                                f"{quoted_reader_role}"
                            )
                        )
        finally:
            admin_engine.dispose()

    with admin_engine.begin() as connection:
        schema_exists = connection.execute(
            text(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_namespace "
                "WHERE nspname IN (:system_schema, :agent_schema)"
                ")"
            ),
            {
                "system_schema": system_schema,
                "agent_schema": agent_schema,
            },
        ).scalar_one()
        role_exists = connection.execute(
            text(
                "SELECT EXISTS("
                "SELECT 1 FROM pg_roles WHERE rolname = :reader_role"
                ")"
            ),
            {"reader_role": reader_role},
        ).scalar_one()
        if schema_exists or (reader_role_created and role_exists):
            raise RuntimeError(
                "generated browser-test PostgreSQL identifiers already exist"
            )
        if not reader_role_created and not role_exists:
            raise RuntimeError(
                "configured browser PostgreSQL reader role does not exist"
            )
        connection.execute(text(f'CREATE SCHEMA "{system_schema}"'))
        connection.execute(text(f'CREATE SCHEMA "{agent_schema}"'))
        quoted_reader_role = (
            connection.dialect.identifier_preparer.quote(reader_role)
        )
        if reader_role_created:
            connection.execute(
                text(
                    f"CREATE ROLE {quoted_reader_role} LOGIN "
                    f"PASSWORD '{reader_password}' "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                )
            )
            connection.execute(
                text(
                    f"ALTER ROLE {quoted_reader_role} "
                    "SET default_transaction_read_only = on"
                )
            )
            connection.execute(
                text(
                    f"ALTER ROLE {quoted_reader_role} "
                    f'SET search_path = "{system_schema}"'
                )
            )
        if reader_role_created:
            connection.execute(
                text(
                    f"GRANT CONNECT ON DATABASE {quoted_database} "
                    f"TO {quoted_reader_role}"
                )
            )
    objects_created = True
    atexit.register(cleanup_database_objects)

    system_url = _url_with_search_path(parsed_url, system_schema)
    agent_url = _url_with_search_path(parsed_url, agent_schema)
    reader_base_url = (
        configured_reader_url
        if configured_reader_url is not None
        else parsed_url.set(
            username=reader_role,
            password=reader_password,
        )
    )
    reader_url = _reader_url_with_search_path(
        reader_base_url,
        system_schema,
    )
    system_port = _free_port()
    agent_port = _free_port()
    system_base_url = f"http://127.0.0.1:{system_port}"
    agent_base_url = f"http://127.0.0.1:{agent_port}"
    run_id = f"{int(time.time() * 1000)}-{unique_suffix[:8]}"
    output_root = ROOT / "output" / "playwright" / f"v13-real-service-{run_id}"
    output_root.mkdir(parents=True, exist_ok=True)

    processes: list[tuple[str, subprocess.Popen[str], Any]] = []
    result_code = 1
    with tempfile.TemporaryDirectory(
        prefix="da-drug-v13-browser-"
    ) as temp_directory:
        temp_path = Path(temp_directory)
        empty_env_file = temp_path / "empty.env"
        empty_env_file.write_text("", encoding="utf-8")
        encryption_key = base64.urlsafe_b64encode(
            os.urandom(32)
        ).decode("ascii")
        environment = _sanitized_child_environment()
        environment.update(
            {
                "APP_ENV": "testbed",
                "DA_DRUG_ENV_FILE": str(empty_env_file),
                "PATIENT_ID": "patient_0000000000000001",
                "TESTBED_RESET_ENABLED": "true",
                "SYSTEM_DATABASE_URL": system_url,
                "SYSTEM_MIGRATION_DATABASE_URL": system_url,
                "SYSTEM_STARTUP_MIGRATIONS_ENABLED": "false",
                "AGENT_DATABASE_URL": agent_url,
                "AGENT_MIGRATION_DATABASE_URL": agent_url,
                "AGENT_STARTUP_MIGRATIONS_ENABLED": "false",
                "BACKEND_READ_DATABASE_URL": reader_url,
                "SYSTEM_BASE_URL": system_base_url,
                "AGENT_BASE_URL": agent_base_url,
                "INTERNAL_API_TOKEN": "browser-v13-internal-token",
                "AGENT_SYNC_API_TOKEN": "browser-v13-agent-token",
                "AGENT_FEEDBACK_ENCRYPTION_KEY": encryption_key,
                "AGENT_FEEDBACK_ENCRYPTION_KEY_ID": "browser-v13",
                "LLM_PROVIDER": "deterministic_test",
                "LLM_MODEL_TIER": "fast",
                "DETERMINISTIC_TEST_PROVIDER_DELAY_MS": "350",
                "DETERMINISTIC_TEST_STREAM_FAILURE_MARKER": (
                    "[stream-failure-after-token]"
                ),
                "DETERMINISTIC_TEST_STREAM_FAILURE_DELAY_MS": "500",
                "DETERMINISTIC_TEST_STREAM_FAILURE_ONCE": "true",
                "DETERMINISTIC_TEST_BURST_STREAM_MARKER": (
                    "[burst-token-stream]"
                ),
                "DETERMINISTIC_TEST_BURST_CHUNK_COUNT": "270",
                "DETERMINISTIC_TEST_BURST_CHUNK_SIZE": "512",
                "AGENT_SYNC_MAX_RETRIES": "0",
                "AGENT_SYNC_CHAT_TIMEOUT_SECONDS": "10",
                "AGENT_SYNC_CHAT_TOTAL_TIMEOUT_SECONDS": "12",
                "AGENT_SYNC_FEEDBACK_TOTAL_TIMEOUT_SECONDS": "5",
                "AGENT_EMBEDDED_WORKER_ENABLED": "false",
                "BASE_URL": system_base_url,
                "REAL_LLM_SERVICE_OUTPUT_DIR": str(output_root),
                "P0_REAL_SERVICE_MODE": "deterministic_v13",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )

        try:
            _run_migration(
                "system_app.migrate",
                service_name="system_app",
                environment=environment,
            )
            _grant_backend_reader(
                admin_engine,
                schema_name=system_schema,
                role_name=reader_role,
            )
            _run_migration(
                "agent_app.migrate",
                service_name="agent_app",
                environment=environment,
            )

            agent_environment = {
                **environment,
                "DA_DRUG_SERVICE": "agent_app",
            }
            system_environment = {
                **environment,
                "DA_DRUG_SERVICE": "system_app",
            }
            agent_process, agent_log = _start_process(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "agent_app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(agent_port),
                    "--log-level",
                    "info",
                ],
                environment=agent_environment,
                log_path=output_root / "agent-server.log",
            )
            processes.append(("agent", agent_process, agent_log))
            _wait_for_json(
                f"{agent_base_url}/health/ready",
                process=agent_process,
                predicate=lambda payload: payload.get("status") == "ready",
            )

            system_process, system_log = _start_process(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "system_app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(system_port),
                    "--log-level",
                    "info",
                ],
                environment=system_environment,
                log_path=output_root / "system-server.log",
            )
            processes.append(("system", system_process, system_log))
            _wait_for_json(
                f"{system_base_url}/health",
                process=system_process,
                predicate=lambda payload: payload.get("status") == "ok",
            )
            _post_json(
                f"{system_base_url}/api/ui/v1/clock/pause",
                {},
            )

            worker_process, worker_log = _start_process(
                [sys.executable, "-m", "agent_app.worker_main"],
                environment=agent_environment,
                log_path=output_root / "agent-worker.log",
            )
            processes.append(("agent-worker", worker_process, worker_log))
            _wait_for_json(
                f"{agent_base_url}/agent/async/tasks/status",
                process=worker_process,
                headers={
                    "X-Internal-Api-Token": (
                        environment["INTERNAL_API_TOKEN"]
                    )
                },
                predicate=lambda payload: any(
                    worker.get("status") == "running"
                    for worker in payload.get("workers", [])
                    if isinstance(worker, dict)
                ),
            )

            browser_environment = {
                **environment,
                "AGENT_PROCESS_PID": str(agent_process.pid),
            }
            completed = subprocess.run(
                ["node", "tools/playwright_p0_real_service_validation.js"],
                cwd=ROOT,
                env=browser_environment,
                check=False,
                text=True,
            )
            result_code = completed.returncode
            if result_code == 0:
                evidence = _boundary_evidence(agent_url, system_url)
                validation_dir = (
                    output_root / "p0-real-service-validation"
                )
                (validation_dir / "service-boundary-evidence.json").write_text(
                    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                stream_evidence = _stream_regression_evidence(
                    agent_url,
                    system_url,
                    validation_dir / "summary.json",
                )
                (validation_dir / "stream-regression-evidence.json").write_text(
                    json.dumps(
                        stream_evidence,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
        finally:
            for _, process, _ in reversed(processes):
                _stop_process(process)
            for _, _, log_handle in processes:
                log_handle.close()

    cleanup_database_objects()
    atexit.unregister(cleanup_database_objects)
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
