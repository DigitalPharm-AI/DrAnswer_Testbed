from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = PROJECT_ROOT / "deploy" / "ec2-agent"
SYSTEMD_DIR = DEPLOY_DIR / "systemd"


def load_script_module(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        module_name,
        DEPLOY_DIR / filename,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = load_script_module(
    "ec2_agent_validate_env",
    "validate_env.py",
)
GENERATION_PROBE = load_script_module(
    "ec2_agent_probe_generation",
    "probe_generation.py",
)


def configured_common_env() -> dict[str, str]:
    values = VALIDATOR.read_env(
        DEPLOY_DIR / ".env.agent_app.ec2.example"
    )
    values.update(
        {
            "BEDROCK_AUTH_MODE": "bearer_token",
            "AGENT_DATABASE_URL": (
                "postgresql+psycopg://agent@agent-db.test/dranswer_agent"
            ),
            "AGENT_MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://migrator@agent-db.test/"
                "dranswer_agent"
            ),
            "BACKEND_READ_DATABASE_URL": (
                "postgresql+psycopg://reader@backend-db.test/"
                "dranswer_backend"
            ),
            "SYSTEM_BASE_URL": "https://backend.test",
            "INTERNAL_API_TOKEN": "a" * 32,
            "BACKEND_API_TOKEN": "b" * 32,
            "AGENT_SYNC_API_TOKEN": "c" * 32,
            "AGENT_FEEDBACK_ENCRYPTION_KEY": (
                base64.urlsafe_b64encode(b"d" * 32)
                .decode("ascii")
                .rstrip("=")
            ),
            "LANGFUSE_EXPORT_ENABLED": "false",
        }
    )
    return values


def test_bedrock_bearer_is_valid_only_in_secret_scope() -> None:
    common = configured_common_env()
    secret = {
        "AWS_BEARER_TOKEN_BEDROCK": "synthetic-bedrock-token",
    }

    assert VALIDATOR.validate(common, secret) == []

    common["AWS_BEARER_TOKEN_BEDROCK"] = ""
    errors = VALIDATOR.validate(common, secret)
    assert any(
        "AWS_BEARER_TOKEN_BEDROCK: forbidden in common agent.env"
        in error
        for error in errors
    )
    assert "synthetic-bedrock-token" not in "\n".join(errors)


@pytest.mark.parametrize(
    ("auth_mode", "secret_value", "expected_fragment"),
    [
        (
            "bearer_token",
            "",
            "AWS_BEARER_TOKEN_BEDROCK: missing from bedrock.env",
        ),
        (
            "bearer_token",
            "CHANGE_ME",
            "placeholder remains in bedrock.env",
        ),
        (
            "iam_role",
            "synthetic-bedrock-token",
            "must be empty when BEDROCK_AUTH_MODE=iam_role",
        ),
        (
            "unknown",
            "",
            "BEDROCK_AUTH_MODE: must be bearer_token or iam_role",
        ),
    ],
)
def test_bedrock_auth_mode_fails_closed(
    auth_mode: str,
    secret_value: str,
    expected_fragment: str,
) -> None:
    common = configured_common_env()
    common["BEDROCK_AUTH_MODE"] = auth_mode
    errors = VALIDATOR.validate(
        common,
        {"AWS_BEARER_TOKEN_BEDROCK": secret_value},
    )

    assert any(expected_fragment in error for error in errors)
    assert "synthetic-bedrock-token" not in "\n".join(errors)


def test_iam_role_mode_accepts_an_empty_or_absent_secret_file() -> None:
    common = configured_common_env()
    common["BEDROCK_AUTH_MODE"] = "iam_role"

    assert VALIDATOR.validate(common, {}) == []
    assert VALIDATOR.validate(
        common,
        {"AWS_BEARER_TOKEN_BEDROCK": ""},
    ) == []


def test_bedrock_secret_file_rejects_unrelated_configuration() -> None:
    errors = VALIDATOR.validate(
        configured_common_env(),
        {
            "AWS_BEARER_TOKEN_BEDROCK": "synthetic-bedrock-token",
            "SYSTEM_BASE_URL": "https://wrong-scope.test",
        },
    )

    assert "SYSTEM_BASE_URL: not allowed in bedrock.env" in errors


def test_only_api_and_worker_units_load_bedrock_secret_file() -> None:
    unit_sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in SYSTEMD_DIR.glob("*.service")
    }
    secret_environment_line = (
        "EnvironmentFile=-/etc/dranswer-agent/bedrock.env"
    )

    assert secret_environment_line in unit_sources[
        "dranswer-agent-api.service"
    ]
    assert secret_environment_line in unit_sources[
        "dranswer-agent-worker.service"
    ]
    for unit_name in (
        "dranswer-agent-migrate.service",
        "dranswer-agent-langfuse-exporter.service",
    ):
        assert "bedrock.env" not in unit_sources[unit_name]
        assert (
            "UnsetEnvironment=AWS_BEARER_TOKEN_BEDROCK"
            in unit_sources[unit_name]
        )
    assert all(
        "EnvironmentFile=/etc/dranswer-agent/agent.env" in source
        for source in unit_sources.values()
    )


def test_secret_installers_do_not_accept_token_arguments_or_disable_host_keys(
) -> None:
    remote_script = (
        DEPLOY_DIR / "install_bedrock_token.sh"
    ).read_text(encoding="utf-8")
    powershell_helper = (
        DEPLOY_DIR / "install_bedrock_token_remote.ps1"
    ).read_text(encoding="utf-8")
    release_installer = (
        DEPLOY_DIR / "install_release.sh"
    ).read_text(encoding="utf-8")

    assert "if [[ $# -ne 0 ]]" in remote_script
    assert remote_script.startswith("#!/usr/bin/env bash\nset +x\n")
    assert "read -r -s token" in remote_script
    assert "sudo -n install" in remote_script
    assert "-m 0600" in remote_script
    assert "-o root" in remote_script
    assert "-g root" in remote_script
    assert "StrictHostKeyChecking=yes" in powershell_helper
    assert "BatchMode=yes" in powershell_helper
    assert "RedirectStandardInput = $true" in powershell_helper
    assert "$process.StandardInput.Write($token)" in powershell_helper
    assert "StrictHostKeyChecking=no" not in powershell_helper
    assert "UserKnownHostsFile=/dev/null" not in powershell_helper
    assert "[string]$Token," not in powershell_helper
    assert "[string]$ReleaseId" in powershell_helper
    assert "/opt/dranswer-agent/releases/$ReleaseId/" in powershell_helper
    assert "install -m 0600 -o root -g root" in release_installer


@pytest.mark.parametrize(
    ("status", "expected_exit_code"),
    [(200, 0), (401, 1), (503, 1)],
)
def test_generation_probe_status_is_fail_closed_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
    expected_exit_code: int,
) -> None:
    synthetic_secret = "synthetic-agent-sync-secret"
    env_file = tmp_path / "agent.env"
    env_file.write_text(
        f"AGENT_SYNC_API_TOKEN={synthetic_secret}\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        GENERATION_PROBE,
        "probe_generation",
        lambda **_kwargs: (
            status,
            (
                '{"status":"ready"}'
                if status == 200
                else f'{{"reflected":"{synthetic_secret}"}}'
            ),
        ),
    )

    exit_code = GENERATION_PROBE.main(
        [
            str(env_file),
            "--base-url",
            "http://127.0.0.1:8701",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == expected_exit_code
    assert synthetic_secret not in captured.out
    assert synthetic_secret not in captured.err
    if status != 200:
        assert f"HTTP {status}" in captured.err


def test_generation_probe_sends_auth_only_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_limit: int) -> bytes:
            return b'{"status":"ready"}'

    class FakeConnection:
        def __init__(
            self,
            hostname: str,
            port: int,
            *,
            timeout: float,
        ) -> None:
            captured["target"] = (hostname, port, timeout)

        @staticmethod
        def request(
            method: str,
            path: str,
            *,
            headers: dict[str, str],
        ) -> None:
            captured["request"] = (method, path, headers)

        @staticmethod
        def getresponse() -> FakeResponse:
            return FakeResponse()

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(
        GENERATION_PROBE.http.client,
        "HTTPConnection",
        FakeConnection,
    )

    status, _body = GENERATION_PROBE.probe_generation(
        base_url="http://127.0.0.1:8701",
        token="synthetic-agent-sync-secret",
        timeout_seconds=10,
    )

    assert status == 200
    assert captured["target"] == ("127.0.0.1", 8701, 10)
    method, path, headers = captured["request"]
    assert method == "GET"
    assert path == "/health/generation/ready"
    assert headers["Authorization"] == (
        "Bearer synthetic-agent-sync-secret"
    )
    with pytest.raises(ValueError, match="loopback"):
        GENERATION_PROBE.generation_readiness_url(
            "http://backend.example"
        )


def test_activate_and_verify_validate_both_scopes_and_probe_generation() -> None:
    activation = (DEPLOY_DIR / "activate.sh").read_text(encoding="utf-8")
    verification = (DEPLOY_DIR / "verify.sh").read_text(encoding="utf-8")
    common_path = "/etc/dranswer-agent/agent.env"
    secret_path = "/etc/dranswer-agent/bedrock.env"

    assert common_path in activation
    assert secret_path in activation
    assert common_path in verification
    assert secret_path in verification
    assert "probe_generation.py" in verification
    assert "Authorization:" not in verification
    assert "/health/generation/ready" not in verification


def test_bundle_templates_cannot_carry_a_real_bedrock_token() -> None:
    common_template = (
        DEPLOY_DIR / ".env.agent_app.ec2.example"
    ).read_text(encoding="utf-8")
    bedrock_template = (
        DEPLOY_DIR / ".env.bedrock.ec2.example"
    ).read_text(encoding="utf-8")
    bundle_script = (
        DEPLOY_DIR / "build_release_bundle.py"
    ).read_text(encoding="utf-8")

    assert "AWS_BEARER_TOKEN_BEDROCK=" not in common_template
    assert bedrock_template.count("AWS_BEARER_TOKEN_BEDROCK=") == 1
    assert "AWS_BEARER_TOKEN_BEDROCK=\n" in bedrock_template
    assert (
        "Bedrock environment template must contain one empty token assignment"
        in bundle_script
    )
    assert "clean Git commit" in bundle_script
