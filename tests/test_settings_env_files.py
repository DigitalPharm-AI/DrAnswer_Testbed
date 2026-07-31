import pytest

from shared.settings import Settings, normalize_model_tier, settings_env_files


def test_settings_env_files_include_agent_app_override(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (".env.agent_app",)


def test_settings_env_files_unknown_service_loads_no_dotenv(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "retired_agent")

    assert settings_env_files() == ()


def test_settings_env_files_do_not_accept_retired_app_service_alias(
    monkeypatch,
    tmp_path,
):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.delenv("DA_DRUG_SERVICE", raising=False)
    monkeypatch.setenv("APP_SERVICE", "agent_app")

    assert settings_env_files() == ()


def test_settings_env_files_include_system_app_override(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "system-app")

    assert settings_env_files() == (".env.system_app",)


def test_settings_env_files_allow_explicit_override(monkeypatch):
    monkeypatch.setenv("DA_DRUG_ENV_FILE", ".env.shared,.env.local")
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (".env.shared", ".env.local")


def test_settings_env_files_allow_agent_overlay_for_local_testbed(monkeypatch):
    monkeypatch.setenv(
        "DA_DRUG_ENV_FILE",
        ".env.9000,.env.agent_app.secret",
    )
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (
        ".env.9000",
        ".env.agent_app.secret",
    )


def test_default_model_tier_is_sonnet(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_MODEL_TIER", raising=False)

    settings = Settings()

    assert normalize_model_tier(None) == "sonnet"
    assert settings.llm_model_tier == "sonnet"


def test_settings_env_files_do_not_discover_parent_shared_env(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    child = parent / "DA_drug"
    child.mkdir(parents=True)
    (parent / ".env").write_text(
        "LLM_PROVIDER=ignored-parent-provider\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(child)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (".env.agent_app",)


@pytest.mark.parametrize("app_env", ["development", "testbed", "production"])
def test_every_environment_requires_internal_api_token(app_env):
    settings = Settings(app_env=app_env, internal_api_token="")

    with pytest.raises(RuntimeError, match="INTERNAL_API_TOKEN"):
        settings.require_internal_api_token()


def test_internal_api_token_returns_configured_value():
    settings = Settings(
        app_env="development",
        internal_api_token="internal-secret",
    )

    assert settings.require_internal_api_token() == "internal-secret"


@pytest.mark.parametrize("app_env", ["development", "testbed", "production"])
def test_agent_sync_api_token_is_required_in_every_environment(app_env):
    settings = Settings(
        app_env=app_env,
        agent_sync_api_token="",
        backend_api_token="backend-secret",
    )

    with pytest.raises(RuntimeError, match="AGENT_SYNC_API_TOKEN"):
        settings.require_agent_sync_api_token()


@pytest.mark.parametrize("app_env", ["development", "testbed", "production"])
def test_backend_api_token_is_required_in_every_environment(app_env):
    settings = Settings(
        app_env=app_env,
        agent_sync_api_token="agent-sync-secret",
        backend_api_token="",
    )

    with pytest.raises(RuntimeError, match="BACKEND_API_TOKEN"):
        settings.require_backend_api_token()


def test_v13_direction_tokens_must_use_distinct_values():
    settings = Settings(
        app_env="development",
        agent_sync_api_token="shared-secret",
        backend_api_token="shared-secret",
    )

    with pytest.raises(RuntimeError, match="must use distinct values"):
        settings.require_agent_sync_api_token()
    with pytest.raises(RuntimeError, match="must use distinct values"):
        settings.require_backend_api_token()


def _chdir_without_parent_env(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    child = parent / "DA_drug"
    child.mkdir(parents=True)
    monkeypatch.chdir(child)
