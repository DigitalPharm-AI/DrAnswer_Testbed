from __future__ import annotations

import os
from typing import Any

from agent_app.providers.base import BaseLLMProvider
from agent_app.providers.config import get_runtime_model_tier
from shared.settings import get_settings

NO_TEMPERATURE_MODEL_MARKERS = ("claude-sonnet-5",)


class BedrockAnthropicProvider(BaseLLMProvider):
    def __init__(self) -> None:
        self.settings = get_settings()

    def chat_model(self):
        try:
            import boto3
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
            raise RuntimeError("langchain-aws and boto3 are required for ChatModel.bind_tools() with Bedrock.") from exc

        if self.settings.aws_bearer_token_bedrock:
            os.environ["AWS_BEARER_TOKEN_BEDROCK"] = self.settings.aws_bearer_token_bedrock

        model_id = self.settings.model_id_for_tier(get_runtime_model_tier())
        session = boto3.Session(**self._session_kwargs())
        client = session.client("bedrock-runtime")
        kwargs = {
            "client": client,
            "model": model_id,
            "max_tokens": self.settings.llm_max_tokens,
        }
        if model_supports_temperature(model_id):
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


def model_supports_temperature(model_id: str) -> bool:
    normalized = model_id.strip().lower()
    return not any(marker in normalized for marker in NO_TEMPERATURE_MODEL_MARKERS)
