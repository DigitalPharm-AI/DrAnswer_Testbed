import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import run_ci_browser_test
from tools import run_v13_real_service_browser_test


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STACK_SCRIPT = PROJECT_ROOT / "tools" / "da_drug_9000_stack.ps1"
PLAYWRIGHT_RUNNER = PROJECT_ROOT / "tools" / "run_real_llm_playwright.ps1"
EC2_RUNNER = PROJECT_ROOT / "scripts" / "run_ec2_services.sh"
DOCKER_RUNNER = PROJECT_ROOT / "scripts" / "docker_compose.sh"
BEDROCK_BEARER_ENV_KEY = "AWS_BEARER_TOKEN_BEDROCK"


def _powershell_function(source: str, name: str) -> str:
    start = source.index(f"function {name}")
    next_function = source.find("\nfunction ", start + 1)
    if next_function == -1:
        return source[start:]
    return source[start:next_function]


def test_9000_stack_scopes_agent_overlay_to_agent_api_and_worker() -> None:
    source = STACK_SCRIPT.read_text(encoding="utf-8")
    service_specs = _powershell_function(source, "Service-Specs")
    start_one = _powershell_function(source, "Start-One")

    assert '[string]$AgentEnvFile = ".env.agent_app.secret"' in source
    assert '$agentRuntimeEnvFiles = "$EnvFile,$AgentEnvFile"' in service_specs
    assert service_specs.count("EnvFiles = $EnvFile") == 1
    assert service_specs.count("EnvFiles = $agentRuntimeEnvFiles") == 2
    assert "$env:DA_DRUG_ENV_FILE = $Spec.EnvFiles" in start_one
    assert f"$env:{BEDROCK_BEARER_ENV_KEY} = $null" in start_one
    assert (
        f"$env:{BEDROCK_BEARER_ENV_KEY} = $previousBedrockBearer"
        in start_one
    )


def test_9000_migrations_and_verifier_are_common_only_and_scrub_parent_bearer() -> None:
    source = STACK_SCRIPT.read_text(encoding="utf-8")
    for function_name in ("Invoke-AgentMigration", "Invoke-SystemMigration"):
        function_source = _powershell_function(source, function_name)
        assert "$env:DA_DRUG_ENV_FILE = $EnvFile" in function_source
        assert "$AgentEnvFile" not in function_source
        assert f"$env:{BEDROCK_BEARER_ENV_KEY} = $null" in function_source
        assert (
            f"$env:{BEDROCK_BEARER_ENV_KEY} = $previousBedrockBearer"
            in function_source
        )

    verifier = _powershell_function(
        source,
        "Invoke-BoundaryContractVerification",
    )
    assert "--env-file $EnvFile" in verifier
    assert "$AgentEnvFile" not in verifier
    assert f"$env:{BEDROCK_BEARER_ENV_KEY} = $null" in verifier
    assert (
        f"$env:{BEDROCK_BEARER_ENV_KEY} = $previousBedrockBearer"
        in verifier
    )
    for function_name in ("Invoke-LocalPostgres", "Build-ReactFrontend"):
        function_source = _powershell_function(source, function_name)
        assert f"$env:{BEDROCK_BEARER_ENV_KEY} = $null" in function_source
        assert (
            f"$env:{BEDROCK_BEARER_ENV_KEY} = $previousBedrockBearer"
            in function_source
        )


def test_9000_common_bearer_guard_is_key_based_and_generation_probe_remains() -> None:
    source = STACK_SCRIPT.read_text(encoding="utf-8")
    guard = _powershell_function(
        source,
        "Assert-CommonEnvCredentialBoundary",
    )
    verify_stack = _powershell_function(source, "Verify-Stack")

    assert "$commonEnv.ContainsKey($BedrockBearerEnvKey)" in guard
    assert '$commonEnv[$BedrockBearerEnvKey]' not in guard
    assert "Assert-CommonEnvCredentialBoundary" in verify_stack
    assert "/health/generation/ready" in verify_stack
    assert "env_file = $EnvFile" in verify_stack
    assert "$AgentEnvFile" not in verify_stack


@pytest.mark.parametrize("bearer_value", ["", "synthetic-sensitive-marker"])
def test_9000_common_bearer_guard_fails_without_value_leak(
    tmp_path: Path,
    bearer_value: str,
) -> None:
    powershell = shutil.which("powershell.exe") or shutil.which("pwsh")
    if powershell is None:
        pytest.skip("PowerShell is unavailable")
    common_env = tmp_path / "common.env"
    agent_env = tmp_path / "agent.secret"
    common_env.write_text(
        f"{BEDROCK_BEARER_ENV_KEY}={bearer_value}\n",
        encoding="utf-8",
    )
    agent_env.write_text("", encoding="utf-8")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(STACK_SCRIPT),
            "-Action",
            "verify",
            "-EnvFile",
            str(common_env),
            "-AgentEnvFile",
            str(agent_env),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode != 0
    assert BEDROCK_BEARER_ENV_KEY in output
    if bearer_value:
        assert bearer_value not in output


def test_playwright_runner_forwards_overlay_and_scrubs_parent_bearer() -> None:
    source = PLAYWRIGHT_RUNNER.read_text(encoding="utf-8")

    assert '[string]$AgentEnvFile = ".env.agent_app.secret"' in source
    assert source.count("-AgentEnvFile $AgentEnvFile") == 2
    assert f"$env:{BEDROCK_BEARER_ENV_KEY} = $null" in source
    assert (
        f"$env:{BEDROCK_BEARER_ENV_KEY} = $previousBedrockBearer"
        in source
    )


def test_shell_runners_keep_agent_secret_out_of_migrations_and_system() -> None:
    ec2_source = EC2_RUNNER.read_text(encoding="utf-8")
    docker_source = DOCKER_RUNNER.read_text(encoding="utf-8")

    assert (
        'ensure_secret_env_pair \\\n'
        '    ".env.agent_app.secret" \\\n'
        '    ".env.agent_app.secret.example"'
        in ec2_source
    )
    assert 'load_service_env "agent_app" "true"' in ec2_source
    assert 'load_service_env "agent_app"\n' in ec2_source
    assert "unset AWS_BEARER_TOKEN_BEDROCK" in ec2_source
    assert (
        'ensure_secret_env_pair \\\n'
        '    ".env.agent_app.secret" \\\n'
        '    ".env.agent_app.secret.example"'
        in docker_source
    )
    assert (
        'assert_common_env_has_no_bedrock_bearer ".env.agent_app"'
        in docker_source
    )
    assert (
        'assert_common_env_has_no_bedrock_bearer ".env.system_app"'
        in docker_source
    )
    assert "grep -Eq" in docker_source
    for source in (ec2_source, docker_source):
        assert "umask 077" in source
        assert 'chmod 600 "$target"' in source


def test_shell_runners_gate_real_generation_without_token_in_argv() -> None:
    ec2_source = EC2_RUNNER.read_text(encoding="utf-8")
    docker_source = DOCKER_RUNNER.read_text(encoding="utf-8")

    assert (
        "exec -T agent-app python - <<'PY'"
        in docker_source
    )
    assert (
        'os.environ.get("AGENT_SYNC_API_TOKEN", "").strip()'
        in docker_source
    )
    assert "/health/generation/ready" in docker_source
    assert docker_source.count("verify_agent_generation") >= 5
    assert "print(token" not in docker_source
    assert '"$PYTHON" - "$AGENT_PORT" <<\'PY\'' in ec2_source
    assert (
        'os.environ.get("AGENT_SYNC_API_TOKEN", "").strip()'
        in ec2_source
    )
    assert "/health/generation/ready" in ec2_source
    assert ec2_source.count("generation_healthcheck") >= 4
    assert "print(token" not in ec2_source


def test_browser_runners_strip_synthetic_ambient_bearer(
    monkeypatch,
) -> None:
    marker = "synthetic-parent-bearer-marker"
    monkeypatch.setenv(BEDROCK_BEARER_ENV_KEY, marker)

    for runner in (
        run_ci_browser_test,
        run_v13_real_service_browser_test,
    ):
        child_environment = runner._sanitized_child_environment()
        assert BEDROCK_BEARER_ENV_KEY not in child_environment
    assert os.environ[BEDROCK_BEARER_ENV_KEY] == marker
