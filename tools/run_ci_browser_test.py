from __future__ import annotations

import atexit
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

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"


def _sanitized_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop(BEDROCK_BEARER_ENV_KEY, None)
    return environment


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return int(server_socket.getsockname()[1])


def _wait_until_ready(
    base_url: str,
    process: subprocess.Popen[str],
    *,
    log_path: Path,
    timeout_seconds: float = 120.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = (
                log_path.read_text(encoding="utf-8", errors="replace")
                if log_path.exists()
                else ""
            )
            raise RuntimeError(f"system_app exited before browser test:\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if 200 <= response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    output = (
        log_path.read_text(encoding="utf-8", errors="replace")
        if log_path.exists()
        else ""
    )
    raise TimeoutError(
        f"system_app did not become ready at {base_url}:\n{output}"
    )


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
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
    schema_name = f"browser_ci_{uuid.uuid4().hex}"
    admin_engine = create_engine(parsed_url, future=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))

    def cleanup_schema() -> None:
        try:
            with admin_engine.begin() as connection:
                connection.execute(
                    text(
                        f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'
                    )
                )
        finally:
            admin_engine.dispose()

    atexit.register(cleanup_schema)
    query = dict(parsed_url.query)
    query["options"] = f"-csearch_path={schema_name}"
    isolated_url = parsed_url.set(query=query).render_as_string(
        hide_password=False
    )
    result_code = 1
    with tempfile.TemporaryDirectory(prefix="da-drug-browser-ci-") as temp_directory:
        temp_path = Path(temp_directory)
        empty_env_file = temp_path / "empty.env"
        empty_env_file.write_text("", encoding="utf-8")
        environment = _sanitized_child_environment()
        environment.update(
            {
                "APP_ENV": "testbed",
                "DA_DRUG_SERVICE": "system_app",
                "DA_DRUG_ENV_FILE": str(empty_env_file),
                "PATIENT_ID": "patient_0000000000000001",
                "TESTBED_RESET_ENABLED": "true",
                "SYSTEM_DATABASE_URL": isolated_url,
                "SYSTEM_MIGRATION_DATABASE_URL": isolated_url,
                "SYSTEM_STARTUP_MIGRATIONS_ENABLED": "false",
                "PROMPT_WORKBOOK_PATH": str(temp_path / "prompt_registry.xlsx"),
                "POLICY_WORKBOOK_PATH": str(temp_path / "notification_policies.xlsx"),
                "PRO_CTCAE_WORKBOOK_PATH": str(temp_path / "pro_ctcae.xlsx"),
                "SYSTEM_BASE_URL": base_url,
                "AGENT_BASE_URL": "http://127.0.0.1:9",
                "INTERNAL_API_TOKEN": "browser-ci-internal-token",
                "AGENT_SYNC_API_TOKEN": "browser-ci-agent-token",
                "LLM_PROVIDER": "deterministic_test",
                "LLM_MODEL_TIER": "fast",
                "BASE_URL": base_url,
                "BROWSER_ARTIFACT_DIR": str(
                    temp_path / "browser-artifacts"
                ),
                "PYTHONUNBUFFERED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "system_app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "info",
        ]
        migration = subprocess.run(
            [sys.executable, "-m", "system_app.migrate"],
            cwd=ROOT,
            env=environment,
            check=False,
            text=True,
        )
        if migration.returncode != 0:
            raise RuntimeError(
                "system_app PostgreSQL migration failed before browser test"
            )
        server_log_path = temp_path / "system_app.log"
        server_log_handle = server_log_path.open("w", encoding="utf-8")
        try:
            server = subprocess.Popen(
                command,
                cwd=ROOT,
                env=environment,
                stdout=server_log_handle,
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
        except Exception:
            server_log_handle.close()
            raise
        try:
            _wait_until_ready(
                base_url,
                server,
                log_path=server_log_path,
            )
            completed = subprocess.run(
                ["node", "tools/playwright_ci_smoke.js"],
                cwd=ROOT,
                env=environment,
                check=False,
                text=True,
            )
            result_code = completed.returncode
        finally:
            if server.poll() is None:
                if os.name == "nt":
                    # Let uvicorn run its shutdown hooks before removing the
                    # isolated PostgreSQL schema.
                    server.send_signal(signal.CTRL_BREAK_EVENT)
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        subprocess.run(
                            [
                                "taskkill",
                                "/PID",
                                str(server.pid),
                                "/T",
                                "/F",
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                        )
                        server.kill()
                        server.wait(timeout=5)
                else:
                    server.terminate()
                    try:
                        server.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        server.kill()
                        server.wait(timeout=5)
            server_log_handle.close()
            if result_code != 0 and server_log_path.exists():
                print(
                    server_log_path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    ),
                    file=sys.stderr,
                )
    cleanup_schema()
    atexit.unregister(cleanup_schema)
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
