from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server_socket:
        server_socket.bind(("127.0.0.1", 0))
        return int(server_socket.getsockname()[1])


def _wait_until_ready(base_url: str, process: subprocess.Popen[str], timeout_seconds: float = 45.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout is not None else ""
            raise RuntimeError(f"system_app exited before browser test:\n{output}")
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=1.0) as response:
                if 200 <= response.status < 500:
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError(f"system_app did not become ready at {base_url}")


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="da-drug-browser-ci-") as temp_directory:
        temp_path = Path(temp_directory)
        empty_env_file = temp_path / "empty.env"
        empty_env_file.write_text("", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "APP_ENV": "development",
                "DA_DRUG_SERVICE": "system_app",
                "DA_DRUG_ENV_FILE": str(empty_env_file),
                "PATIENT_ID": "browser-ci-patient",
                "SYSTEM_DATABASE_URL": f"sqlite:///{(temp_path / 'system.db').as_posix()}",
                "AGENT_DATABASE_URL": f"sqlite:///{(temp_path / 'agent.db').as_posix()}",
                "PHR_DATABASE_URL": f"sqlite:///{(temp_path / 'phr.db').as_posix()}",
                "PROMPT_WORKBOOK_PATH": str(temp_path / "prompt_registry.xlsx"),
                "POLICY_WORKBOOK_PATH": str(temp_path / "notification_policies.xlsx"),
                "PRO_CTCAE_WORKBOOK_PATH": str(temp_path / "pro_ctcae.xlsx"),
                "SYSTEM_BASE_URL": base_url,
                "AGENT_BASE_URL": "http://127.0.0.1:9",
                "PHR_BASE_URL": "http://127.0.0.1:9",
                "INTERNAL_API_TOKEN": "browser-ci-internal-token",
                "BACKEND_API_TOKEN": "browser-ci-backend-token",
                "AGENT_SYNC_API_TOKEN": "browser-ci-agent-token",
                "LLM_PROVIDER": "rule_based",
                "LLM_MODEL_TIER": "fast",
                "BASE_URL": base_url,
                "BROWSER_ARTIFACT_DIR": str(ROOT / "artifacts" / "browser-ci"),
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
            "warning",
        ]
        server = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            _wait_until_ready(base_url, server)
            completed = subprocess.run(
                ["node", "tools/playwright_ci_smoke.js"],
                cwd=ROOT,
                env=environment,
                check=False,
                text=True,
            )
            return completed.returncode
        finally:
            if server.poll() is None:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=5)
            if server.returncode not in (0, -15, 1) and server.stdout is not None:
                print(server.stdout.read(), file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
