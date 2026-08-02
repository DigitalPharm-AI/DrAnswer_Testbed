from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class ContractServerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "testbed"
    app_release_version: str = "local"
    log_level: str = "INFO"
    contract_host: str = "127.0.0.1"
    contract_port: int = Field(default=8701, ge=1, le=65535)
    contract_database_url: str = (
        "postgresql+psycopg://contract_app_rw@127.0.0.1:5432/"
        "dranswer_contract"
    )
    contract_migration_database_url: str = (
        "postgresql+psycopg://contract_migration@127.0.0.1:5432/"
        "dranswer_contract"
    )
    contract_startup_migrations_enabled: bool = False
    contract_db_pool_size: int = Field(default=5, ge=1)
    contract_db_max_overflow: int = Field(default=10, ge=0)
    contract_db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    contract_db_statement_timeout_ms: int = Field(default=30_000, ge=0)
    contract_db_lock_timeout_ms: int = Field(default=5_000, ge=0)
    contract_specs_dir: Path = Path("docs")
    contract_stream_chunk_delay_ms: int = Field(
        default=150,
        ge=50,
        le=5_000,
    )
    contract_stream_delta_chunks: int = Field(
        default=4,
        ge=2,
        le=20,
    )

    agent_sync_api_token: str = ""
    test_control_token: str = ""
    feedback_digest_secret: str = ""

    callback_mode: Literal["hold", "deliver"] = "hold"
    backend_callback_base_url: str = ""
    allow_insecure_backend_http: bool = False
    callback_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    callback_max_attempts: int = Field(default=3, ge=1, le=20)
    callback_retry_base_seconds: float = Field(default=5.0, ge=0.1, le=3600)
    callback_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)

    @property
    def agent_auth_configured(self) -> bool:
        return len(self.agent_sync_api_token) >= 32

    @property
    def test_control_configured(self) -> bool:
        return len(self.test_control_token) >= 32

    @property
    def feedback_digest_configured(self) -> bool:
        return len(self.feedback_digest_secret) >= 32

    @property
    def callback_delivery_configured(self) -> bool:
        if not self.agent_auth_configured:
            return False
        parsed = urlparse(self.backend_callback_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            return False
        if parsed.scheme == "http" and not self.allow_insecure_backend_http:
            return False
        return True

    @property
    def initial_callback_status(self) -> Literal["held", "pending"]:
        if self.callback_mode == "deliver" and self.callback_delivery_configured:
            return "pending"
        return "held"

    @staticmethod
    def _is_postgresql_url(database_url: str) -> bool:
        try:
            return make_url(
                database_url.strip()
            ).drivername.startswith("postgresql")
        except Exception:
            return False

    def require_contract_postgresql(self) -> None:
        if not self._is_postgresql_url(self.contract_database_url):
            raise RuntimeError(
                "CONTRACT_DATABASE_URL must use PostgreSQL."
            )
        if self.contract_startup_migrations_enabled:
            raise RuntimeError(
                "CONTRACT_STARTUP_MIGRATIONS_ENABLED must remain false; "
                "run `python -m contract_test_server.migrate` before "
                "starting the service."
            )

    def require_contract_migration_postgresql(self) -> None:
        if not self._is_postgresql_url(
            self.contract_migration_database_url
        ):
            raise RuntimeError(
                "CONTRACT_MIGRATION_DATABASE_URL must use PostgreSQL."
            )

    @property
    def readiness_errors(self) -> list[str]:
        errors: list[str] = []
        if not self._is_postgresql_url(self.contract_database_url):
            errors.append("contract_database_postgresql_required")
        if self.contract_startup_migrations_enabled:
            errors.append("contract_startup_migrations_must_be_disabled")
        if not self.agent_auth_configured:
            errors.append("agent_sync_api_token_not_configured")
        if not self.test_control_configured:
            errors.append("test_control_token_not_configured")
        if not self.feedback_digest_configured:
            errors.append("feedback_digest_secret_not_configured")
        configured_secrets = [
            value
            for value in (
                self.agent_sync_api_token,
                self.test_control_token,
                self.feedback_digest_secret,
            )
            if value
        ]
        if len(configured_secrets) != len(set(configured_secrets)):
            errors.append("service_secrets_must_be_distinct")
        if self.callback_mode == "deliver" and not self.callback_delivery_configured:
            errors.append("callback_delivery_not_configured")
        return errors


@lru_cache(maxsize=1)
def get_contract_server_settings() -> ContractServerSettings:
    return ContractServerSettings()
