from __future__ import annotations

import asyncio
import json
import os
from time import perf_counter
from typing import Any
from urllib.parse import quote

import httpx

from agent_app.provider_base import BaseLLMProvider
from agent_app.provider_config import get_runtime_model_tier
from agent_app.provider_json import parse_json_object
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

    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        model_id = self.settings.model_id_for_tier(get_runtime_model_tier())
        started = perf_counter()
        try:
            if self.settings.aws_bearer_token_bedrock:
                payload = await asyncio.to_thread(self._converse_with_bearer_token, model_id, system_prompt, user_payload)
                content = self._content_from_converse_response(payload)
            else:
                payload = await asyncio.to_thread(self._invoke_model, model_id, system_prompt, user_payload)
                content = self._content_from_messages_response(payload)
            return parse_json_object(content)
        except Exception as exc:
            elapsed_ms = round((perf_counter() - started) * 1000)
            raise RuntimeError(f"bedrock_anthropic_failed elapsed_ms={elapsed_ms} error={exc}") from exc

    @staticmethod
    def _content_from_messages_response(payload: dict[str, Any]) -> str:
        content_blocks = payload.get("content")
        if not isinstance(content_blocks, list):
            raise ValueError("Bedrock Anthropic response does not contain content blocks.")
        return "".join(str(block.get("text", "")) for block in content_blocks if isinstance(block, dict) and block.get("type") == "text")

    @staticmethod
    def _content_from_converse_response(payload: dict[str, Any]) -> str:
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        message = output.get("message") if isinstance(output.get("message"), dict) else {}
        content_blocks = message.get("content")
        if not isinstance(content_blocks, list):
            raise ValueError("Bedrock Converse response does not contain output.message.content blocks.")
        return "".join(str(block.get("text", "")) for block in content_blocks if isinstance(block, dict) and block.get("text"))

    def _invoke_model(self, model_id: str, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on optional runtime dependency
            raise RuntimeError("boto3 is required for LLM_PROVIDER=bedrock_anthropic.") from exc

        session = boto3.Session(**self._session_kwargs())
        client = session.client("bedrock-runtime")
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": self.settings.llm_max_tokens,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": json.dumps(user_payload, ensure_ascii=False)}],
                }
            ],
        }
        if model_supports_temperature(model_id):
            body["temperature"] = self.settings.llm_temperature
        response = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            accept="application/json",
            contentType="application/json",
        )
        return json.loads(response["body"].read())

    def _converse_with_bearer_token(self, model_id: str, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        encoded_model_id = quote(model_id, safe=":.-_")
        url = f"https://bedrock-runtime.{self.settings.aws_region}.amazonaws.com/model/{encoded_model_id}/converse"
        inference_config = {"maxTokens": self.settings.llm_max_tokens}
        if model_supports_temperature(model_id):
            inference_config["temperature"] = self.settings.llm_temperature
        body = {
            "system": [{"text": system_prompt}],
            "messages": [{"role": "user", "content": [{"text": json.dumps(user_payload, ensure_ascii=False)}]}],
            "inferenceConfig": inference_config,
        }
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.settings.aws_bearer_token_bedrock}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.settings.llm_timeout_seconds) as client:
            response = client.post(url, json=body, headers=headers)
            if response.is_error:
                raise RuntimeError(f"Bedrock bearer call failed: HTTP {response.status_code} {response.text[:500]}")
            return response.json()

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
