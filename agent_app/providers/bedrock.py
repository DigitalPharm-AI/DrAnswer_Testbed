from __future__ import annotations

import os
from threading import Lock
from time import monotonic
from typing import Any

from agent_app.providers.base import BaseLLMProvider, GenerationReadiness
from agent_app.providers.config import get_runtime_model_tier
from shared.settings import get_settings

NO_TEMPERATURE_MODEL_MARKERS = ("claude-sonnet-5",)
HAIKU_EXTENDED_THINKING_MODEL_MARKERS = ("claude-haiku-4-5",)
SONNET_ADAPTIVE_THINKING_MODEL_MARKERS = ("claude-sonnet-4-6",)
MIN_EXTENDED_THINKING_BUDGET_TOKENS = 1024
GENERATION_READINESS_TTL_SECONDS = 300.0
GENERATION_READINESS_TIMEOUT_SECONDS = 3.0


class BedrockAnthropicProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.settings = get_settings()
        self._readiness_lock = Lock()
        self._readiness_checked_at = float("-inf")
        self._readiness_result = GenerationReadiness(
            ok=False,
            code="GENERATION_PROVIDER_NOT_CHECKED",
        )

    def chat_model(self):
        try:
            import boto3
            from botocore.config import Config
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
            raise RuntimeError("langchain-aws and boto3 are required for ChatModel.bind_tools() with Bedrock.") from exc

        bearer_token = self._bearer_token()
        if bearer_token:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer_token

        model_id = self.settings.model_id_for_tier(get_runtime_model_tier())
        session = boto3.Session(**self._session_kwargs())
        timeout_seconds = max(
            0.1,
            float(self.settings.llm_timeout_seconds),
        )
        client = session.client(
            "bedrock-runtime",
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 0},
            ),
        )
        reasoning_fields = reasoning_request_fields(
            model_id,
            enabled=self.settings.llm_reasoning_enabled,
            effort=self.settings.llm_reasoning_effort,
            extended_thinking_budget_tokens=(
                self.settings.llm_extended_thinking_budget_tokens
            ),
            max_tokens=self.settings.llm_max_tokens,
        )
        kwargs = {
            "client": client,
            "model": model_id,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if reasoning_fields:
            kwargs["additional_model_request_fields"] = reasoning_fields
        if model_supports_temperature(
            model_id,
            reasoning_enabled=bool(reasoning_fields),
        ):
            kwargs["temperature"] = self.settings.llm_temperature
        return ChatBedrockConverse(**kwargs)

    def _session_kwargs(self) -> dict[str, Any]:
        session_kwargs: dict[str, Any] = {"region_name": self.settings.aws_region}
        if self.settings.aws_profile:
            session_kwargs["profile_name"] = self.settings.aws_profile
        if self.settings.aws_access_key_id and self.settings.aws_secret_access_key:
            session_kwargs["aws_access_key_id"] = self.settings.aws_access_key_id
            session_kwargs["aws_secret_access_key"] = self.settings.aws_secret_access_key
        if self.settings.aws_session_token:
            session_kwargs["aws_session_token"] = self.settings.aws_session_token
        return session_kwargs

    def _bearer_token(self) -> str:
        value = self.settings.aws_bearer_token_bedrock
        if value is None:
            return ""
        get_secret_value = getattr(value, "get_secret_value", None)
        if callable(get_secret_value):
            value = get_secret_value()
        return str(value).strip()

    def generation_readiness(self) -> GenerationReadiness:
        now = monotonic()
        if now - self._readiness_checked_at < GENERATION_READINESS_TTL_SECONDS:
            return self._readiness_result
        with self._readiness_lock:
            now = monotonic()
            if now - self._readiness_checked_at < GENERATION_READINESS_TTL_SECONDS:
                return self._readiness_result
            self._readiness_result = self._probe_generation()
            self._readiness_checked_at = monotonic()
            return self._readiness_result

    def configuration_readiness(self) -> GenerationReadiness:
        try:
            import boto3  # noqa: F401
            import langchain_aws  # noqa: F401
        except ImportError:
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_DEPENDENCY_MISSING",
            )
        model_id = self.settings.model_id_for_tier(
            get_runtime_model_tier()
        ).strip()
        if not model_id or not self.settings.aws_region.strip():
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_CONFIGURATION_INVALID",
            )
        return GenerationReadiness(
            ok=True,
            code="GENERATION_PROVIDER_CONFIGURED",
        )

    def _probe_generation(self) -> GenerationReadiness:
        """Perform a minimal real Bedrock invocation without exposing its output."""

        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import (
                NoCredentialsError,
                PartialCredentialsError,
            )
        except ImportError:
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_DEPENDENCY_MISSING",
            )

        bearer_token = self._bearer_token()
        if bearer_token:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = bearer_token

        try:
            session = boto3.Session(**self._session_kwargs())
            if (
                not bearer_token
                and session.get_credentials() is None
            ):
                return GenerationReadiness(
                    ok=False,
                    code="GENERATION_PROVIDER_CREDENTIALS_UNAVAILABLE",
                )
            client = session.client(
                "bedrock-runtime",
                config=Config(
                    connect_timeout=GENERATION_READINESS_TIMEOUT_SECONDS,
                    read_timeout=GENERATION_READINESS_TIMEOUT_SECONDS,
                    retries={"max_attempts": 0},
                ),
            )
            response = client.converse(
                modelId=self.settings.model_id_for_tier(
                    get_runtime_model_tier()
                ),
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": "Reply with OK."}],
                    }
                ],
                inferenceConfig={"maxTokens": 1},
            )
        except (NoCredentialsError, PartialCredentialsError):
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_CREDENTIALS_UNAVAILABLE",
            )
        except Exception:
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_INVOCATION_FAILED",
            )
        content = (
            response.get("output", {})
            .get("message", {})
            .get("content", [])
            if isinstance(response, dict)
            else []
        )
        if not any(
            isinstance(item, dict)
            and isinstance(item.get("text"), str)
            and bool(item["text"].strip())
            for item in content
        ):
            return GenerationReadiness(
                ok=False,
                code="GENERATION_PROVIDER_RESPONSE_INVALID",
            )
        return GenerationReadiness(ok=True, code="OK")


def reasoning_request_fields(
    model_id: str,
    *,
    enabled: bool,
    effort: str,
    extended_thinking_budget_tokens: int,
    max_tokens: int,
) -> dict[str, Any]:
    """Build model-specific Bedrock thinking fields for supported Claude models."""

    normalized = model_id.strip().lower()
    if not enabled:
        return {}
    if any(
        marker in normalized
        for marker in HAIKU_EXTENDED_THINKING_MODEL_MARKERS
    ):
        budget_tokens = int(extended_thinking_budget_tokens)
        if budget_tokens < MIN_EXTENDED_THINKING_BUDGET_TOKENS:
            raise ValueError(
                "llm_extended_thinking_budget_tokens_must_be_at_least_1024"
            )
        if budget_tokens >= int(max_tokens):
            raise ValueError(
                "llm_extended_thinking_budget_tokens_must_be_less_than_"
                "llm_max_tokens"
            )
        return {
            "thinking": {
                "type": "enabled",
                "budget_tokens": budget_tokens,
            }
        }
    if any(
        marker in normalized
        for marker in SONNET_ADAPTIVE_THINKING_MODEL_MARKERS
    ):
        normalized_effort = effort.strip().lower()
        if normalized_effort not in {"low", "medium", "high"}:
            raise ValueError(f"unsupported_llm_reasoning_effort:{effort}")
        return {
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": normalized_effort,
            },
        }
    return {}


def model_supports_temperature(
    model_id: str,
    *,
    reasoning_enabled: bool = False,
) -> bool:
    if reasoning_enabled:
        return False
    normalized = model_id.strip().lower()
    return not any(marker in normalized for marker in NO_TEMPERATURE_MODEL_MARKERS)
