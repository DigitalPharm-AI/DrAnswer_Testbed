import base64
import binascii
import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from shared.public_ids import PatientId

MODEL_TIERS = ("fast", "sonnet")
PRODUCTION_ENVS = {"prod", "production"}
SERVICE_ENV_FILES = {
    "agent_app": ".env.agent_app",
    "system_app": ".env.system_app",
}


def normalize_model_tier(value: str | None) -> str:
    tier = (value or "sonnet").strip().lower()
    if tier not in MODEL_TIERS:
        raise ValueError(f"unsupported_model_tier:{value}")
    return tier


def settings_env_files() -> tuple[str, ...]:
    explicit = os.getenv("DA_DRUG_ENV_FILE")
    if explicit:
        return tuple(part.strip() for part in explicit.replace(";", ",").split(",") if part.strip())

    service_name = (os.getenv("DA_DRUG_SERVICE") or "").strip().lower().replace("-", "_")
    service_env_file = SERVICE_ENV_FILES.get(service_name)
    if service_env_file:
        return (service_env_file,)
    return ()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=settings_env_files(), env_file_encoding="utf-8", extra="ignore")

    app_env: str = "development"
    app_release_version: str = ""
    patient_id: PatientId = "patient_0000000000000001"
    testbed_reset_enabled: bool = True

    system_database_url: str = (
        "postgresql+psycopg://backend_app_rw@127.0.0.1:5432/"
        "dranswer_backend"
    )
    system_migration_database_url: str = (
        "postgresql+psycopg://backend_migration@127.0.0.1:5432/"
        "dranswer_backend"
    )
    system_startup_migrations_enabled: bool = False
    agent_database_url: str = (
        "postgresql+psycopg://agent_app_rw@127.0.0.1:5432/"
        "dranswer_agent"
    )
    agent_migration_database_url: str = (
        "postgresql+psycopg://agent_migration@127.0.0.1:5432/"
        "dranswer_agent"
    )
    agent_startup_migrations_enabled: bool = False
    system_db_pool_size: int = 5
    system_db_max_overflow: int = 10
    system_db_pool_timeout_seconds: float = 30.0
    system_db_pool_recycle_seconds: int = 1_800
    system_db_statement_timeout_ms: int = 30_000
    system_db_lock_timeout_ms: int = 5_000
    agent_db_pool_size: int = 5
    agent_db_max_overflow: int = 10
    agent_db_pool_timeout_seconds: float = 30.0
    agent_db_pool_recycle_seconds: int = 1_800
    agent_db_statement_timeout_ms: int = 30_000
    agent_db_lock_timeout_ms: int = 5_000
    system_base_url: str = "http://127.0.0.1:8000"
    agent_base_url: str = "http://127.0.0.1:8001"
    backend_read_database_url: str = (
        "postgresql+psycopg://ai_backend_reader@127.0.0.1:5432/"
        "dranswer_backend"
    )
    backend_query_max_rows: int = 100
    backend_read_db_pool_size: int = 5
    backend_read_db_max_overflow: int = 5
    backend_read_db_pool_timeout_seconds: float = 10.0
    backend_read_db_pool_recycle_seconds: int = 1_800
    backend_read_db_statement_timeout_ms: int = 10_000
    backend_read_db_lock_timeout_ms: int = 2_000
    backend_record_change_path: str = "/agent/sync/record-change"
    backend_notification_policy_change_path: str = "/agent/sync/notification-policy-change"
    backend_api_token: str | None = None
    backend_api_timeout_seconds: float = 90.0
    backend_api_max_retries: int = 2
    agent_sync_api_token: str | None = None
    agent_sync_chat_timeout_seconds: float = 90.0
    agent_sync_chat_total_timeout_seconds: float = Field(
        default=100.0,
        gt=0,
        le=100.0,
    )
    agent_sync_feedback_total_timeout_seconds: float = Field(
        default=15.0,
        gt=0,
        le=15.0,
    )
    agent_sync_max_retries: int = 2
    ui_agent_status_cache_ttl_seconds: float = 60.0
    agent_sync_lock_lease_seconds: int = 120
    agent_sync_request_retention_seconds: int = 86_400
    agent_sync_retry_after_seconds: int = 1
    agent_async_task_retention_seconds: int = Field(
        default=2_592_000,
        ge=86_400,
    )
    agent_retention_cleanup_interval_seconds: int = Field(
        default=3_600,
        ge=60,
    )
    agent_backend_write_retention_seconds: int = 2_592_000
    agent_feedback_retention_seconds: int = 604_800
    agent_feedback_max_attempts: int = 3
    agent_feedback_processing_lease_seconds: int = 300
    agent_feedback_retry_base_seconds: int = 60
    agent_feedback_retry_max_seconds: int = 3_600
    agent_feedback_encryption_key: SecretStr | None = None
    agent_feedback_encryption_key_id: str = "feedback-v1"
    agent_pro_ctcae_survey_ttl_seconds: int = Field(
        default=86_400,
        ge=300,
    )
    agent_pro_ctcae_survey_retention_seconds: int = Field(
        default=94_608_000,
        ge=86_400,
    )
    agent_food_selection_ttl_seconds: int = Field(
        default=86_400,
        ge=300,
    )
    agent_food_selection_retention_seconds: int = Field(
        default=94_608_000,
        ge=86_400,
    )
    internal_api_token: str | None = None
    prompt_workbook_path: Path = Field(default=Path("data/prompt_registry.xlsx"))
    policy_workbook_path: Path = Field(default=Path("data/default_notification_policies.xlsx"))
    pro_ctcae_workbook_path: Path = Field(default=Path("data/pro_ctcae_korean_parsed.xlsx"))
    pro_ctcae_similarity_threshold: float = 0.56
    llm_provider: str = "bedrock_anthropic"
    llm_model_tier: str = "sonnet"
    llm_fast_model: str = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    llm_sonnet_model: str = "global.anthropic.claude-sonnet-4-6"
    llm_reasoning_enabled: bool = True
    llm_reasoning_effort: Literal["low", "medium", "high"] = "medium"
    llm_extended_thinking_budget_tokens: int = Field(default=1024, ge=1024)
    llm_timeout_seconds: int = 60
    llm_max_tokens: int = 4096
    llm_temperature: float = 0.2
    agent_trace_logging: bool = True
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
    pilot_ui_status_p95_ms: int = 2000
    agent_daily_cost_budget_usd: float = 50.0
    agent_eval_cost_budget_usd: float = 10.0
    agent_cost_input_usd_per_1m_tokens: float = 0.0
    agent_cost_output_usd_per_1m_tokens: float = 0.0
    langfuse_export_enabled: bool = False
    langfuse_base_url: str = ""
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr | None = None
    langfuse_verify_tls: bool = True
    langfuse_request_timeout_seconds: float = Field(
        default=10.0,
        gt=0,
        le=60,
    )
    langfuse_export_batch_size: int = Field(default=20, ge=1, le=200)
    langfuse_export_poll_seconds: float = Field(
        default=2.0,
        ge=0.1,
        le=60,
    )
    langfuse_export_max_attempts: int = Field(default=12, ge=1, le=100)
    langfuse_export_lease_seconds: int = Field(default=300, ge=10, le=3_600)
    langfuse_export_retry_base_seconds: int = Field(
        default=5,
        ge=1,
        le=3_600,
    )
    langfuse_export_retry_max_seconds: int = Field(
        default=900,
        ge=1,
        le=86_400,
    )
    langfuse_export_circuit_failure_threshold: int = Field(
        default=5,
        ge=1,
        le=100,
    )
    langfuse_export_circuit_cooldown_seconds: int = Field(
        default=60,
        ge=1,
        le=3_600,
    )
    langfuse_success_sample_rate: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
    )
    langfuse_outbox_retention_seconds: int = Field(
        default=2_592_000,
        ge=86_400,
    )
    langfuse_retention_days: int = Field(default=1_095, ge=3)

    aws_region: str = "ap-northeast-2"
    aws_profile: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_bearer_token_bedrock: SecretStr | None = None

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
    @model_validator(mode="after")
    def validate_active_llm_thinking_budget(self) -> "Settings":
        active_model = self.model_id_for_tier(self.llm_model_tier).strip().lower()
        if (
            self.llm_reasoning_enabled
            and "claude-haiku-4-5" in active_model
            and self.llm_extended_thinking_budget_tokens >= self.llm_max_tokens
        ):
            raise ValueError(
                "LLM_EXTENDED_THINKING_BUDGET_TOKENS must be less than "
                "LLM_MAX_TOKENS when Haiku extended thinking is active."
            )
        return self

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

    def require_internal_api_token(self) -> str:
        token = (self.internal_api_token or "").strip()
        if not token:
            raise RuntimeError(
                "INTERNAL_API_TOKEN is required in every environment."
            )
        return token

    def require_agent_sync_api_token(self) -> str:
        token = (self.agent_sync_api_token or "").strip()
        if not token:
            raise RuntimeError("AGENT_SYNC_API_TOKEN is required in every environment.")
        backend_token = (self.backend_api_token or "").strip()
        if backend_token and hmac.compare_digest(token, backend_token):
            raise RuntimeError("AGENT_SYNC_API_TOKEN and BACKEND_API_TOKEN must use distinct values.")
        return token

    def require_backend_api_token(self) -> str:
        token = (self.backend_api_token or "").strip()
        if not token:
            raise RuntimeError("BACKEND_API_TOKEN is required in every environment.")
        agent_sync_token = (self.agent_sync_api_token or "").strip()
        if agent_sync_token and hmac.compare_digest(token, agent_sync_token):
            raise RuntimeError("AGENT_SYNC_API_TOKEN and BACKEND_API_TOKEN must use distinct values.")
        return token

    def require_backend_read_database_url(self) -> None:
        if not self.backend_read_database_url.strip():
            raise RuntimeError(
                "BACKEND_READ_DATABASE_URL is required in every environment."
            )

    def require_agent_postgresql(self) -> None:
        if _database_dialect(self.agent_database_url) != "postgresql":
            raise RuntimeError(
                "AGENT_DATABASE_URL must use PostgreSQL in every environment."
            )
        if self.agent_startup_migrations_enabled:
            raise RuntimeError(
                "AGENT_STARTUP_MIGRATIONS_ENABLED must remain false; run "
                "`python -m agent_app.migrate` before starting the service."
            )

    def require_backend_read_postgresql(self) -> None:
        self.require_backend_read_database_url()
        if _database_dialect(self.backend_read_database_url) != "postgresql":
            raise RuntimeError(
                "BACKEND_READ_DATABASE_URL must use PostgreSQL in every "
                "environment."
            )

    def require_system_postgresql(self) -> None:
        if _database_dialect(self.system_database_url) != "postgresql":
            raise RuntimeError(
                "SYSTEM_DATABASE_URL must use PostgreSQL in every environment."
            )
        if self.system_startup_migrations_enabled:
            raise RuntimeError(
                "SYSTEM_STARTUP_MIGRATIONS_ENABLED must remain false; run "
                "`python -m system_app.migrate` before starting the service."
            )

    def require_agent_feedback_encryption(self) -> tuple[str, bytes]:
        encoded = (
            self.agent_feedback_encryption_key.get_secret_value().strip()
            if self.agent_feedback_encryption_key is not None
            else ""
        )
        if not encoded:
            raise RuntimeError(
                "AGENT_FEEDBACK_ENCRYPTION_KEY is required in every environment."
            )
        try:
            key = base64.b64decode(
                encoded + ("=" * (-len(encoded) % 4)),
                altchars=b"-_",
                validate=True,
            )
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError(
                "AGENT_FEEDBACK_ENCRYPTION_KEY must be URL-safe base64."
            ) from exc
        if len(key) != 32:
            raise RuntimeError(
                "AGENT_FEEDBACK_ENCRYPTION_KEY must decode to exactly 32 bytes."
            )
        key_id = self.agent_feedback_encryption_key_id.strip()
        if not key_id:
            raise RuntimeError(
                "AGENT_FEEDBACK_ENCRYPTION_KEY_ID is required."
            )
        if len(key_id) > 120:
            raise RuntimeError(
                "AGENT_FEEDBACK_ENCRYPTION_KEY_ID must be at most 120 characters."
            )
        return key_id, key

    def require_langfuse_export_config(self) -> tuple[str, str, str]:
        if not self.langfuse_export_enabled:
            raise RuntimeError("LANGFUSE_EXPORT_ENABLED is false.")
        base_url = self.langfuse_base_url.strip().rstrip("/")
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise RuntimeError("LANGFUSE_BASE_URL is invalid.") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError(
                "LANGFUSE_BASE_URL must be a valid http(s) URL."
            )
        public_key = self.langfuse_public_key.strip()
        secret_key = (
            self.langfuse_secret_key.get_secret_value().strip()
            if self.langfuse_secret_key is not None
            else ""
        )
        if not public_key or not secret_key:
            raise RuntimeError(
                "LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required."
            )
        if (
            self.langfuse_export_retry_max_seconds
            < self.langfuse_export_retry_base_seconds
        ):
            raise RuntimeError(
                "LANGFUSE_EXPORT_RETRY_MAX_SECONDS must be greater than or "
                "equal to LANGFUSE_EXPORT_RETRY_BASE_SECONDS."
            )
        minimum_lease = int(
            self.langfuse_export_batch_size
            * self.langfuse_request_timeout_seconds
        ) + 30
        if self.langfuse_export_lease_seconds < minimum_lease:
            raise RuntimeError(
                "LANGFUSE_EXPORT_LEASE_SECONDS is too short for the "
                "configured batch size and request timeout."
            )
        if self.langfuse_retention_days != 1_095:
            raise RuntimeError(
                "LANGFUSE_RETENTION_DAYS must be 1095 to match the Agent "
                "observability retention policy."
            )
        return base_url, public_key, secret_key


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.prompt_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    settings.policy_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    settings.pro_ctcae_workbook_path.parent.mkdir(parents=True, exist_ok=True)
    return settings


def _database_dialect(database_url: str) -> str:
    try:
        driver = make_url(database_url.strip()).drivername
    except Exception as exc:
        raise RuntimeError("database URL is invalid") from exc
    if driver.startswith("postgresql"):
        return "postgresql"
    return driver
