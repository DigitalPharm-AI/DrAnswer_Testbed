import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MODEL_TIERS = ("fast", "sonnet")
PRODUCTION_ENVS = {"prod", "production"}
SERVICE_ENV_FILES = {
    "agent_app": ".env.agent_app",
    "system_app": ".env.system_app",
    "phr_app": ".env.phr_app",
}


def normalize_model_tier(value: str | None) -> str:
    tier = (value or "fast").strip().lower()
    if tier not in MODEL_TIERS:
        raise ValueError(f"unsupported_model_tier:{value}")
    return tier


def settings_env_files() -> tuple[str, ...]:
    explicit = os.getenv("DA_DRUG_ENV_FILE") or os.getenv("APP_ENV_FILE")
    if explicit:
        return tuple(part.strip() for part in explicit.replace(";", ",").split(",") if part.strip())

    shared_env_file = _shared_env_file()
    service_name = (os.getenv("DA_DRUG_SERVICE") or os.getenv("APP_SERVICE") or "").strip().lower().replace("-", "_")
    service_env_file = SERVICE_ENV_FILES.get(service_name)
    if service_env_file:
        return (shared_env_file, service_env_file)
    return (shared_env_file,)


def _shared_env_file() -> str:
    local_env = Path(".env")
    if local_env.exists():
        return ".env"
    parent_env = Path("..") / ".env"
    if parent_env.exists():
        return str(parent_env)
    return ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=settings_env_files(), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    patient_id: str = "demo-patient"

    system_database_url: str = "sqlite:///./data/system.db"
    agent_database_url: str = "sqlite:///./data/agent.db"
    phr_database_url: str = "sqlite:///./data/phr.db"
    phr_read_only: bool = False
    system_base_url: str = "http://127.0.0.1:8000"
    agent_base_url: str = "http://127.0.0.1:8001"
    phr_base_url: str = "http://127.0.0.1:8002"
    internal_api_token: str | None = None
    prompt_workbook_path: Path = Field(default=Path("data/prompt_registry.xlsx"))
    policy_workbook_path: Path = Field(default=Path("data/default_notification_policies.xlsx"))
    pro_ctcae_workbook_path: Path = Field(default=Path("data/pro_ctcae_korean_parsed.xlsx"))
    pro_ctcae_similarity_threshold: float = 0.56
    pro_ctcae_embedding_provider: str = "auto"
    pro_ctcae_embedding_threshold: float = 0.68
    pro_ctcae_embedding_model: str = "amazon.titan-embed-text-v2:0"
    voyage_api_key: str | None = None
    voyage_base_url: str = "https://api.voyageai.com/v1"

    llm_provider: str = "bedrock_anthropic"
    llm_model_tier: str = "fast"
    llm_fast_model: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    llm_sonnet_model: str = "global.anthropic.claude-sonnet-4-6"
    llm_timeout_seconds: int = 60
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.2
    agent_trace_logging: bool = True
    system_trace_logging: bool = True
    phr_trace_logging: bool = True
    agent_task_max_attempts: int = 3
    agent_task_visibility_timeout_seconds: int = 300
    agent_task_retry_base_seconds: int = 30
    agent_task_retry_max_seconds: int = 600
    agent_embedded_worker_enabled: bool = False
    agent_worker_heartbeat_interval_seconds: int = 10
    agent_worker_stale_after_seconds: int = 60
    pilot_load_concurrency_target: int = 50
    pilot_max_error_rate: float = 0.01
    pilot_health_p95_ms: int = 1000
    pilot_async_accept_p95_ms: int = 2000
    pilot_phr_register_p95_ms: int = 3000
    agent_daily_cost_budget_usd: float = 50.0
    agent_eval_cost_budget_usd: float = 10.0
    agent_cost_input_usd_per_1m_tokens: float = 0.0
    agent_cost_output_usd_per_1m_tokens: float = 0.0

    aws_region: str = "ap-northeast-2"
    aws_profile: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_bearer_token_bedrock: str | None = None

    simulation_initial_time: str = "2026-04-20T08:00:00"
    missed_dose_grace_minutes: int = 90
    policy_max_extra_reminders: int = 5
    policy_min_interval_minutes: int = 1
    policy_max_interval_minutes: int = 120
    policy_min_missed_dose_after_minutes: int = 1
    policy_max_missed_dose_after_minutes: int = 240
    policy_max_primary_reminder_offset_minutes: int = 120
    policy_default_extra_reminders: int = 1
    policy_default_interval_minutes: int = 30
    policy_default_duration_days: int = 7

    def model_id_for_tier(self, model_tier: str | None = None) -> str:
        tier = normalize_model_tier(model_tier or self.llm_model_tier)
        if tier == "sonnet":
            return self.llm_sonnet_model
        return self.llm_fast_model

    def available_model_tiers(self) -> dict[str, str]:
        return {
            "fast": self.llm_fast_model,
            "sonnet": self.llm_sonnet_model,
        }

    def is_production(self) -> bool:
        return self.app_env.strip().lower() in PRODUCTION_ENVS

    def require_internal_api_token_in_production(self) -> None:
        if self.is_production() and not (self.internal_api_token or "").strip():
            raise RuntimeError("INTERNAL_API_TOKEN is required when APP_ENV=production.")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prompt_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    settings.policy_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    settings.pro_ctcae_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    return settings
