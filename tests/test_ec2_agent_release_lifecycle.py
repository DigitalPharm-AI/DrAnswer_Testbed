from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tarfile
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


BUILD = load_script_module(
    "ec2_agent_build_release_bundle",
    "build_release_bundle.py",
)
VERIFY_ARTIFACT = load_script_module(
    "ec2_agent_verify_release_artifact",
    "verify_release_artifact.py",
)


def _write(path: Path, value: str | bytes = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, bytes):
        path.write_bytes(value)
    else:
        path.write_text(value, encoding="utf-8")


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.stdout


def _minimal_release_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Release Test")
    _git(repo, "config", "user.email", "release@example.invalid")
    for filename in (
        "main.py",
        "migrate.py",
        "worker_main.py",
    ):
        _write(repo / "agent_app" / filename, f"# {filename}\n")
    _write(repo / "agent_app/llm/prompts.py", "PROMPT_VERSION = 'test'\n")
    _write(
        repo / "shared/backend_read_contract.py",
        "BACKEND_READ_CONTRACT_VERSION = '1.3'\n",
    )
    _write(repo / "data/pro_ctcae_korean_parsed.xlsx", b"synthetic")
    _write(repo / "requirements.txt", "")
    _write(repo / "pyproject.toml", "[project]\nname='synthetic'\n")
    _write(
        repo / "deploy/ec2-agent/requirements.agent.lock",
        "example==1.0\n",
    )
    _write(
        repo / "deploy/ec2-agent/.env.agent_app.ec2.example",
        "\n".join(
            (
                "APP_ENV=testbed",
                "LLM_PROVIDER=bedrock_anthropic",
                "LLM_MODEL_TIER=sonnet",
                "LLM_FAST_MODEL=haiku-test",
                "LLM_SONNET_MODEL=sonnet-test",
                "INTERNAL_API_TOKEN=CHANGE_ME_INTERNAL",
                "BACKEND_API_TOKEN=CHANGE_ME_BACKEND",
                "AGENT_SYNC_API_TOKEN=CHANGE_ME_SYNC",
                "AGENT_FEEDBACK_ENCRYPTION_KEY=CHANGE_ME_FEEDBACK",
                "LANGFUSE_SECRET_KEY=CHANGE_ME_LANGFUSE",
            )
        )
        + "\n",
    )
    _write(
        repo / "deploy/ec2-agent/.env.bedrock.ec2.example",
        "AWS_BEARER_TOKEN_BEDROCK=\n",
    )
    for unit in (
        "dranswer-agent-api.service",
        "dranswer-agent-worker.service",
        "dranswer-agent-migrate.service",
    ):
        _write(repo / "deploy/ec2-agent/systemd" / unit, "[Unit]\n")
    _write(repo / "deploy/ec2-agent/README.md", "# synthetic\n")
    _git(repo, "add", ".")
    commit_env = os.environ.copy()
    commit_env.update(
        {
            "GIT_AUTHOR_DATE": "2026-07-30T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-07-30T00:00:00+00:00",
        }
    )
    _git(repo, "commit", "-q", "-m", "synthetic release", env=commit_env)
    return repo


def test_bundle_is_deterministic_and_bound_to_clean_commit(
    tmp_path: Path,
) -> None:
    repo = _minimal_release_repo(tmp_path)
    first, first_hash, first_manifest = BUILD.build_release_bundle(
        repo_root=repo,
        output_directory=tmp_path / "out-one",
    )
    second, second_hash, second_manifest = BUILD.build_release_bundle(
        repo_root=repo,
        output_directory=tmp_path / "out-two",
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_hash == second_hash
    assert first_manifest == second_manifest
    assert first_manifest["release_id"].startswith("git-")
    assert first_manifest["commit_sha"] == _git(
        repo,
        "rev-parse",
        "HEAD",
    ).strip()
    assert first_manifest["runtime_identifiers"]["llm_sonnet_model"] == (
        "sonnet-test"
    )
    assert VERIFY_ARTIFACT.verify_release_artifact(first) == first_manifest
    with tarfile.open(first, "r:gz") as archive:
        assert "release-manifest.json" in archive.getnames()



@pytest.mark.parametrize(
    "dirty_kind",
    ("modified", "staged", "deleted", "untracked"),
)
def test_bundle_refuses_every_dirty_worktree_state(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    repo = _minimal_release_repo(tmp_path / dirty_kind)
    if dirty_kind == "modified":
        _write(repo / "agent_app/main.py", "# modified\n")
    elif dirty_kind == "staged":
        _write(repo / "agent_app/main.py", "# staged\n")
        _git(repo, "add", "agent_app/main.py")
    elif dirty_kind == "deleted":
        (repo / "agent_app/main.py").unlink()
    else:
        _write(repo / "untracked-secret.txt", "not bundled")

    with pytest.raises(RuntimeError, match="clean Git commit"):
        BUILD.build_release_bundle(
            repo_root=repo,
            output_directory=tmp_path / f"{dirty_kind}-output",
        )


def test_bundle_refuses_a_real_secret_in_the_common_template(
    tmp_path: Path,
) -> None:
    repo = _minimal_release_repo(tmp_path)
    template = repo / "deploy/ec2-agent/.env.agent_app.ec2.example"
    template.write_text(
        template.read_text(encoding="utf-8").replace(
            "INTERNAL_API_TOKEN=CHANGE_ME_INTERNAL",
            "INTERNAL_API_TOKEN=synthetic-real-looking-token",
        ),
        encoding="utf-8",
    )
    _git(repo, "add", str(template.relative_to(repo)))
    _git(repo, "commit", "-q", "-m", "unsafe template")

    with pytest.raises(ValueError, match="contains a value"):
        BUILD.build_release_bundle(
            repo_root=repo,
            output_directory=tmp_path / "unsafe-output",
        )


def test_installed_release_hash_validation_detects_tampering(
    tmp_path: Path,
) -> None:
    repo = _minimal_release_repo(tmp_path)
    archive, _digest, manifest = BUILD.build_release_bundle(
        repo_root=repo,
        output_directory=tmp_path / "out",
    )
    installed = tmp_path / "installed"
    installed.mkdir()
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(installed, filter="data")

    assert (
        VERIFY_ARTIFACT.verify_installed_release(installed)["commit_sha"]
        == manifest["commit_sha"]
    )
    _write(installed / "agent_app/llm/prompts.py", "tampered")
    with pytest.raises(ValueError, match="hash mismatch"):
        VERIFY_ARTIFACT.verify_installed_release(installed)


def test_install_stages_inactive_immutable_release_with_own_venv() -> None:
    installer = (DEPLOY_DIR / "install_release.sh").read_text(
        encoding="utf-8"
    )

    assert 'release_dir="$base_dir/releases/$release_id"' in installer
    assert '"$staging_dir/venv"' in installer
    assert 'mv -- "$staging_dir" "$release_dir"' in installer
    assert 'chown -R root:root "$staging_dir"' in installer
    assert 'chmod -R go-w "$staging_dir"' in installer
    assert 'lock_file="$base_dir/deploy.lock"' in installer
    assert "current" not in "\n".join(
        line
        for line in installer.splitlines()
        if "The current release" not in line
    )
    assert "systemctl start" not in installer
    assert "systemctl enable" not in installer


def test_units_bind_code_dependencies_and_release_identity_to_current() -> None:
    units = {
        path.name: path.read_text(encoding="utf-8")
        for path in SYSTEMD_DIR.glob("*.service")
    }
    for source in units.values():
        assert (
            "EnvironmentFile=/opt/dranswer-agent/current/release.env"
            in source
        )
        assert "/opt/dranswer-agent/current/venv/" in source
        assert "/opt/dranswer-agent/venv/" not in source
    common_template = (
        DEPLOY_DIR / ".env.agent_app.ec2.example"
    ).read_text(encoding="utf-8")
    assert "APP_RELEASE_VERSION=ec2-agent-test" not in common_template
    installer = (DEPLOY_DIR / "install_release.sh").read_text(
        encoding="utf-8"
    )
    assert "APP_RELEASE_VERSION=$release_id" in installer
    assert "AGENT_RELEASE_COMMIT_SHA=$commit_sha" in installer


def test_activation_has_candidate_atomic_switch_and_failure_rollback() -> None:
    activation = (DEPLOY_DIR / "activate.sh").read_text(encoding="utf-8")

    migration_index = activation.index("Running release-specific migration")
    canary_index = activation.index("Starting isolated loopback release canary")
    mutation_index = activation.index("mutation_started=true")
    switch_index = activation.index('atomic_link "$release_dir" "$current_link"')
    verify_index = activation.index(
        'bash "$release_dir/deploy/ec2-agent/verify.sh"'
    )
    assert migration_index < canary_index < mutation_index < switch_index
    assert switch_index < verify_index
    assert "trap on_exit EXIT" in activation
    assert "rollback_failed_activation" in activation
    assert 'sudo -n test -f "$unit_backup_dir/$unit"' in activation
    assert 'sudo -n rm -f -- "$service_dir/$unit"' in activation
    assert 'atomic_link "$previous_release" "$current_link"' in activation
    assert 'sudo -n rm -f -- "$current_link"' in activation
    assert "restore_enable_state" in activation
    assert "previous_active" in activation
    assert "AGENT_DB_BACKUP_ID" in activation
    assert "AGENT_SCHEMA_FORWARD_COMPATIBLE" in activation
    assert "dranswer-agent-api.service" in activation
    assert "dranswer-agent-worker.service" in activation
    assert "system-app" not in activation
    assert "backend" not in activation.lower()


def test_enable_policy_and_verify_cover_reboot_and_ops() -> None:
    activation = (DEPLOY_DIR / "activate.sh").read_text(encoding="utf-8")
    verification = (DEPLOY_DIR / "verify.sh").read_text(encoding="utf-8")

    assert "systemctl enable dranswer-agent-api.service" in activation
    assert "dranswer-agent-worker.service" in activation
    assert "LANGFUSE_EXPORT_ENABLED" in activation
    assert "disable --now" in activation
    assert "systemctl disable dranswer-agent-migrate.service" in activation
    assert "systemctl is-enabled --quiet" in verification
    assert "probe_ops_readiness.py" in verification
    assert "probe_generation.py" in verification
    assert "running_worker_count" in (
        DEPLOY_DIR / "probe_ops_readiness.py"
    ).read_text(encoding="utf-8")
    assert "system-app" not in verification
    assert "backend.service" not in verification


def test_explicit_rollback_restores_release_specific_dependencies() -> None:
    rollback = (DEPLOY_DIR / "rollback.sh").read_text(encoding="utf-8")

    assert "AGENT_SCHEMA_ROLLBACK_COMPATIBLE" in rollback
    assert "Database schema will not be downgraded." in rollback
    assert 'target_dir="$base_dir/releases/$target_id"' in rollback
    assert '"$target_dir/venv/bin/python"' in rollback
    assert 'exec bash "$activation_script" "$target_id"' in rollback


def test_preflight_allows_only_the_existing_agent_listener_for_upgrade() -> None:
    preflight = (DEPLOY_DIR / "preflight.sh").read_text(encoding="utf-8")

    assert "ALLOW_EXISTING_AGENT_UPGRADE" in preflight
    assert "dranswer-agent-api.service" in preflight
    assert "MainPID" in preflight
    assert "listener does not match" in preflight


def _git_bash() -> Path:
    path = Path(r"C:\Program Files\Git\bin\bash.exe")
    if not path.is_file():
        pytest.skip("Git Bash is required for the executable lifecycle harness")
    return path


def _msys_path(bash: Path, path: Path) -> str:
    return subprocess.run(
        [str(bash), "-lc", f"cygpath -u {shlex.quote(str(path))}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _fake_command(path: Path, source: str) -> None:
    _write(path, source.replace("\r\n", "\n"))
    path.chmod(0o755)


def _state_path(state_dir: Path, kind: str, unit: str) -> Path:
    return state_dir / f"{kind}-{unit}"


def _prepare_activation_harness(
    tmp_path: Path,
    *,
    previous_exists: bool,
    previous_exporter_enabled: bool = False,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    bash = _git_bash()
    root = tmp_path / "harness"
    base = root / "opt/dranswer-agent"
    env_dir = root / "etc/dranswer-agent"
    service_dir = root / "etc/systemd/system"
    run_dir = root / "run"
    fake_bin = root / "fake-bin"
    state_dir = root / "state"
    for directory in (
        base / "releases",
        env_dir,
        service_dir,
        run_dir,
        fake_bin,
        state_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _write(base / "deploy.lock", "")
    _write(
        env_dir / "agent.env",
        "LANGFUSE_EXPORT_ENABLED=false\n",
    )
    _write(env_dir / "bedrock.env", "AWS_BEARER_TOKEN_BEDROCK=x\n")

    commit_sha = "a" * 40
    candidate = base / "releases/candidate"
    previous = base / "releases/previous"
    for release, release_id in (
        (candidate, "candidate"),
        (previous, "previous"),
    ):
        (release / "venv/bin").mkdir(parents=True)
        (release / "deploy/ec2-agent/systemd").mkdir(parents=True)
        _fake_command(
            release / "venv/bin/python",
            (
                "#!/usr/bin/env bash\n"
                "for arg in \"$@\"; do\n"
                "  if [[ \"$arg\" == *verify_release_artifact.py ]]; then\n"
                f"    echo '{release_id} {commit_sha}'\n"
                "    exit 0\n"
                "  fi\n"
                "done\n"
                "exit 0\n"
            ),
        )
        _fake_command(
            release / "venv/bin/uvicorn",
            "#!/usr/bin/env bash\nexit 0\n",
        )
        _write(
            release / "release.env",
            (
                f"APP_RELEASE_VERSION={release_id}\n"
                f"AGENT_RELEASE_COMMIT_SHA={commit_sha}\n"
            ),
        )
        _write(release / "release-manifest.json", "{}\n")
        _write(
            release / "deploy/ec2-agent/probe_generation.py",
            "# fake\n",
        )
    for unit in (
        "dranswer-agent-api.service",
        "dranswer-agent-worker.service",
        "dranswer-agent-migrate.service",
        "dranswer-agent-langfuse-exporter.service",
    ):
        _write(
            candidate / "deploy/ec2-agent/systemd" / unit,
            f"new:{unit}\n",
        )
        _write(
            previous / "deploy/ec2-agent/systemd" / unit,
            f"old:{unit}\n",
        )
    _fake_command(
        candidate / "deploy/ec2-agent/verify.sh",
        (
            "#!/usr/bin/env bash\n"
            'if [[ "${FAKE_VERIFY_FAIL:-false}" == "true" ]]; then\n'
            "  exit 91\n"
            "fi\n"
            "exit 0\n"
        ),
    )

    activation_source = (DEPLOY_DIR / "activate.sh").read_text(
        encoding="utf-8"
    )
    replacements = {
        "/opt/dranswer-agent": _msys_path(bash, base),
        "/etc/dranswer-agent": _msys_path(bash, env_dir),
        "/etc/systemd/system": _msys_path(bash, service_dir),
        "/run/dranswer-agent-units": (
            _msys_path(bash, run_dir) + "/dranswer-agent-units"
        ),
    }
    for original, replacement in replacements.items():
        activation_source = activation_source.replace(original, replacement)
    # Git Bash cannot create native directory symlinks on every Windows host.
    # The harness models a symlink as a one-line target marker while exercising
    # the real activation ordering, traps, unit backup, and state restoration.
    activation_source = activation_source.replace(
        '[[ -L "$current_link" ]]',
        '[[ -f "$current_link" ]]',
    )
    activation = candidate / "deploy/ec2-agent/activate.sh"
    _fake_command(activation, activation_source)

    _fake_command(
        fake_bin / "sudo",
        r"""#!/usr/bin/env bash
if [[ "${1:-}" == "-n" ]]; then shift; fi
if [[ "${1:-}" == "install" ]]; then
  shift
  args=()
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -o|-g) shift 2 ;;
      *) args+=("$1"); shift ;;
    esac
  done
  exec install "${args[@]}"
fi
exec "$@"
""",
    )
    for command in ("flock", "systemd-run"):
        _fake_command(
            fake_bin / command,
            "#!/usr/bin/env bash\nexit 0\n",
        )
    _fake_command(
        fake_bin / "ss",
        "#!/usr/bin/env bash\nexit 0\n",
    )
    _fake_command(
        fake_bin / "curl",
        "#!/usr/bin/env bash\nprintf '{}\\n'\nexit 0\n",
    )
    _fake_command(
        fake_bin / "ln",
        r"""#!/usr/bin/env bash
target="${@: -2:1}"
link="${@: -1}"
printf '%s\n' "$target" >"$link"
""",
    )
    _fake_command(
        fake_bin / "readlink",
        r"""#!/usr/bin/env bash
path="${@: -1}"
cat "$path"
""",
    )
    _fake_command(
        fake_bin / "systemctl",
        r"""#!/usr/bin/env bash
set -u
state="${FAKE_STATE_DIR:?}"
cmd="${1:-}"
shift || true
get_state() {
  local kind="$1"
  local unit="$2"
  local file="$state/$kind-$unit"
  [[ -f "$file" ]] && cat "$file" || printf '0'
}
set_state() {
  printf '%s' "$3" >"$state/$1-$2"
}
case "$cmd" in
  is-enabled)
    unit="${@: -1}"
    [[ "$(get_state enabled "$unit")" == "1" ]]
    ;;
  is-active)
    unit="${@: -1}"
    [[ "$(get_state active "$unit")" == "1" ]]
    ;;
  enable|disable|start|stop|restart)
    now=false
    for arg in "$@"; do
      [[ "$arg" == "--now" ]] && { now=true; continue; }
      [[ "$arg" == *.service ]] || continue
      case "$cmd" in
        enable) set_state enabled "$arg" 1 ;;
        disable)
          set_state enabled "$arg" 0
          [[ "$now" == "true" ]] && set_state active "$arg" 0
          ;;
        start|restart) set_state active "$arg" 1 ;;
        stop) set_state active "$arg" 0 ;;
      esac
    done
    ;;
  daemon-reload|reset-failed|status|show)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
""",
    )

    for unit in (
        "dranswer-agent-api.service",
        "dranswer-agent-worker.service",
        "dranswer-agent-langfuse-exporter.service",
    ):
        enabled = (
            previous_exists
            and (
                unit != "dranswer-agent-langfuse-exporter.service"
                or previous_exporter_enabled
            )
        )
        active = enabled
        _write(
            _state_path(state_dir, "enabled", unit),
            "1" if enabled else "0",
        )
        _write(
            _state_path(state_dir, "active", unit),
            "1" if active else "0",
        )
        if previous_exists:
            _write(service_dir / unit, f"old:{unit}\n")
    if previous_exists:
        _write(
            base / "current",
            _msys_path(bash, previous) + "\n",
        )
    env = {
        "AGENT_DB_BACKUP_ID": "synthetic-backup-1",
        "AGENT_SCHEMA_FORWARD_COMPATIBLE": "true",
        "FAKE_STATE_DIR": _msys_path(bash, state_dir),
    }
    return bash, activation, base, service_dir, env


@pytest.mark.parametrize("previous_exists", (True, False))
def test_executable_failure_injection_restores_previous_or_clean_state(
    tmp_path: Path,
    previous_exists: bool,
) -> None:
    bash, activation, base, service_dir, env = _prepare_activation_harness(
        tmp_path,
        previous_exists=previous_exists,
    )
    harness_root = activation.parents[6]
    fake_bin = harness_root / "fake-bin"
    state_dir = harness_root / "state"
    command = (
        f"export PATH={shlex.quote(_msys_path(bash, fake_bin))}:$PATH; "
        f"export FAKE_STATE_DIR={shlex.quote(env['FAKE_STATE_DIR'])}; "
        "export AGENT_DB_BACKUP_ID=synthetic-backup-1; "
        "export AGENT_SCHEMA_FORWARD_COMPATIBLE=true; "
        "export FAKE_VERIFY_FAIL=true; "
        f"bash {shlex.quote(_msys_path(bash, activation))} candidate"
    )
    result = subprocess.run(
        [str(bash), "-lc", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    current = base / "current"
    if previous_exists:
        assert current.read_text().strip().endswith("/releases/previous")
        for unit in (
            "dranswer-agent-api.service",
            "dranswer-agent-worker.service",
            "dranswer-agent-langfuse-exporter.service",
        ):
            assert (service_dir / unit).read_text(encoding="utf-8") == (
                f"old:{unit}\n"
            )
            expected = "0" if "langfuse" in unit else "1"
            assert _state_path(
                state_dir,
                "enabled",
                unit,
            ).read_text() == expected
            assert _state_path(
                state_dir,
                "active",
                unit,
            ).read_text() == expected
    else:
        assert not current.exists()
        assert not any(service_dir.glob("dranswer-agent-*.service"))
        for unit in (
            "dranswer-agent-api.service",
            "dranswer-agent-worker.service",
            "dranswer-agent-langfuse-exporter.service",
        ):
            assert _state_path(
                state_dir,
                "enabled",
                unit,
            ).read_text() == "0"
            assert _state_path(
                state_dir,
                "active",
                unit,
            ).read_text() == "0"
    assert not list(base.glob("*.new.*"))


def test_executable_activation_disables_exporter_when_config_turns_off(
    tmp_path: Path,
) -> None:
    bash, activation, base, service_dir, env = _prepare_activation_harness(
        tmp_path,
        previous_exists=True,
        previous_exporter_enabled=True,
    )
    harness_root = activation.parents[6]
    fake_bin = harness_root / "fake-bin"
    state_dir = harness_root / "state"
    command = (
        f"export PATH={shlex.quote(_msys_path(bash, fake_bin))}:$PATH; "
        f"export FAKE_STATE_DIR={shlex.quote(env['FAKE_STATE_DIR'])}; "
        "export AGENT_DB_BACKUP_ID=synthetic-backup-1; "
        "export AGENT_SCHEMA_FORWARD_COMPATIBLE=true; "
        "export FAKE_VERIFY_FAIL=false; "
        f"bash {shlex.quote(_msys_path(bash, activation))} candidate"
    )
    result = subprocess.run(
        [str(bash), "-lc", command],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert (base / "current").read_text().strip().endswith(
        "/releases/candidate"
    )
    assert (base / "previous").read_text().strip().endswith(
        "/releases/previous"
    )
    for unit in (
        "dranswer-agent-api.service",
        "dranswer-agent-worker.service",
    ):
        assert (service_dir / unit).read_text(encoding="utf-8") == (
            f"new:{unit}\n"
        )
        assert _state_path(state_dir, "enabled", unit).read_text() == "1"
        assert _state_path(state_dir, "active", unit).read_text() == "1"
    exporter = "dranswer-agent-langfuse-exporter.service"
    assert (service_dir / exporter).read_text(encoding="utf-8") == (
        f"new:{exporter}\n"
    )
    assert _state_path(state_dir, "enabled", exporter).read_text() == "0"
    assert _state_path(state_dir, "active", exporter).read_text() == "0"
    migrate = "dranswer-agent-migrate.service"
    assert (service_dir / migrate).read_text(encoding="utf-8") == (
        f"new:{migrate}\n"
    )
    assert _state_path(state_dir, "enabled", migrate).read_text() == "0"
