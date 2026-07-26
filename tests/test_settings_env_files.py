import pytest
from pathlib import Path

from shared.settings import Settings, normalize_model_tier, settings_env_files


def test_settings_env_files_include_agent_app_override(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (".env", ".env.agent_app")


def test_settings_env_files_unknown_service_uses_shared_env(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "retired_agent")

    assert settings_env_files() == (".env",)


def test_settings_env_files_include_system_app_override(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "system-app")

    assert settings_env_files() == (".env", ".env.system_app")


def test_settings_env_files_allow_explicit_override(monkeypatch):
    monkeypatch.setenv("DA_DRUG_ENV_FILE", ".env.shared,.env.local")
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (".env.shared", ".env.local")


def test_default_model_tier_is_sonnet(monkeypatch, tmp_path):
    _chdir_without_parent_env(monkeypatch, tmp_path)
    monkeypatch.delenv("LLM_MODEL_TIER", raising=False)

    settings = Settings()

    assert normalize_model_tier(None) == "sonnet"
    assert settings.llm_model_tier == "sonnet"


def test_settings_env_files_use_parent_shared_env_when_local_env_is_absent(monkeypatch, tmp_path):
    parent = tmp_path / "workspace"
    child = parent / "DA_drug"
    child.mkdir(parents=True)
    (parent / ".env").write_text("LLM_PROVIDER=rule_based\n", encoding="utf-8")
    monkeypatch.chdir(child)
    monkeypatch.delenv("DA_DRUG_ENV_FILE", raising=False)
    monkeypatch.delenv("APP_ENV_FILE", raising=False)
    monkeypatch.setenv("DA_DRUG_SERVICE", "agent_app")

    assert settings_env_files() == (str(Path("..") / ".env"), ".env.agent_app")


def test_production_requires_internal_api_token():
    settings = Settings(app_env="production", internal_api_token="")

    with pytest.raises(RuntimeError, match="INTERNAL_API_TOKEN"):
        settings.require_internal_api_token_in_production()


def test_development_allows_empty_internal_api_token():
    settings = Settings(app_env="development", internal_api_token="")

    settings.require_internal_api_token_in_production()


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


def test_v12_direction_tokens_must_use_distinct_values():
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
