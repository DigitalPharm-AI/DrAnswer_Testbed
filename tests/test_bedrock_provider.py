import os
from types import SimpleNamespace

import boto3
import langchain_aws
import pytest
from pydantic import SecretStr, ValidationError

from agent_app.providers.bedrock import (
    BedrockAnthropicProvider,
    model_supports_temperature,
    reasoning_request_fields,
)
from agent_app.providers.deterministic_test import DeterministicTestProvider
from shared.settings import Settings


def test_bedrock_provider_exposes_only_langchain_chat_model_generation():
    assert not hasattr(BedrockAnthropicProvider, "generate_json")
    assert not hasattr(BedrockAnthropicProvider, "_invoke_model")
    assert not hasattr(BedrockAnthropicProvider, "_converse_with_bearer_token")


def test_bedrock_chat_model_applies_generation_timeout_to_runtime_client(
    monkeypatch,
):
    captured: dict = {}
    expected_model = object()

    class FakeSession:
        def client(self, service_name, *, config):
            captured["service_name"] = service_name
            captured["config"] = config
            return "runtime-client"

    def fake_chat_model(**kwargs):
        captured["model_kwargs"] = kwargs
        return expected_model

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(
        langchain_aws,
        "ChatBedrockConverse",
        fake_chat_model,
    )
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        llm_timeout_seconds=17,
        llm_max_tokens=512,
        llm_temperature=0.2,
        llm_reasoning_enabled=True,
        llm_reasoning_effort="medium",
        llm_extended_thinking_budget_tokens=1024,
        model_id_for_tier=lambda _tier: "test-model",
    )

    model = provider.chat_model()

    assert model is expected_model
    assert captured["service_name"] == "bedrock-runtime"
    assert captured["config"].connect_timeout == 17.0
    assert captured["config"].read_timeout == 17.0
    assert captured["config"].retries["max_attempts"] == 0
    assert captured["model_kwargs"]["client"] == "runtime-client"


def test_bedrock_chat_model_unwraps_secret_bearer_only_at_provider_sink(
    monkeypatch,
):
    marker = "bedrock-chat-secret-marker"
    captured: dict = {}

    class FakeSession:
        def client(self, service_name, *, config):
            assert service_name == "bedrock-runtime"
            assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == marker
            return "runtime-client"

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(
        langchain_aws,
        "ChatBedrockConverse",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=SecretStr(marker),
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        llm_timeout_seconds=17,
        llm_max_tokens=512,
        llm_temperature=0.2,
        llm_reasoning_enabled=False,
        llm_reasoning_effort="medium",
        llm_extended_thinking_budget_tokens=1024,
        model_id_for_tier=lambda _tier: "test-model",
    )

    provider.chat_model()

    assert marker not in repr(captured)
    assert captured["client"] == "runtime-client"


def test_bedrock_configuration_readiness_is_cost_free():
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_region="ap-northeast-2",
        model_id_for_tier=lambda _tier: "test-model",
    )

    readiness = provider.configuration_readiness()

    assert readiness.ok is True
    assert readiness.code == "GENERATION_PROVIDER_CONFIGURED"


def test_claude_sonnet_5_models_do_not_support_temperature():
    assert model_supports_temperature("global.anthropic.claude-sonnet-5") is False


def test_other_anthropic_models_keep_temperature():
    assert model_supports_temperature("global.anthropic.claude-haiku-4-5-20251001-v1:0") is True
    assert model_supports_temperature("global.anthropic.claude-sonnet-4-6") is True


def test_sonnet_46_uses_adaptive_reasoning_without_temperature(monkeypatch):
    captured: dict = {}

    class FakeSession:
        def client(self, _service_name, *, config):
            return object()

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(
        langchain_aws,
        "ChatBedrockConverse",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        llm_timeout_seconds=17,
        llm_max_tokens=4096,
        llm_temperature=0.2,
        llm_reasoning_enabled=True,
        llm_reasoning_effort="medium",
        llm_extended_thinking_budget_tokens=1024,
        model_id_for_tier=lambda _tier: "global.anthropic.claude-sonnet-4-6",
    )

    provider.chat_model()

    assert captured["additional_model_request_fields"] == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }
    assert "output_config" not in captured
    assert "temperature" not in captured
    assert reasoning_request_fields(
        "global.anthropic.claude-sonnet-4-6",
        enabled=True,
        effort="medium",
        extended_thinking_budget_tokens=1024,
        max_tokens=4096,
    ) == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium"},
    }


def test_haiku_45_uses_extended_thinking_without_temperature(monkeypatch):
    captured: dict = {}

    class FakeSession:
        def client(self, _service_name, *, config):
            return object()

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    monkeypatch.setattr(
        langchain_aws,
        "ChatBedrockConverse",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        llm_timeout_seconds=17,
        llm_max_tokens=4096,
        llm_temperature=0.2,
        llm_reasoning_enabled=True,
        llm_reasoning_effort="medium",
        llm_extended_thinking_budget_tokens=2048,
        model_id_for_tier=lambda _tier: (
            "global.anthropic.claude-haiku-4-5-20251001-v1:0"
        ),
    )

    provider.chat_model()

    assert captured["additional_model_request_fields"] == {
        "thinking": {
            "type": "enabled",
            "budget_tokens": 2048,
        }
    }
    assert "output_config" not in captured
    assert "temperature" not in captured


def test_haiku_extended_thinking_rejects_budget_not_below_max_tokens():
    with pytest.raises(
        ValueError,
        match=(
            "llm_extended_thinking_budget_tokens_must_be_less_than_"
            "llm_max_tokens"
        ),
    ):
        reasoning_request_fields(
            "global.anthropic.claude-haiku-4-5-20251001-v1:0",
            enabled=True,
            effort="medium",
            extended_thinking_budget_tokens=4096,
            max_tokens=4096,
        )


def test_settings_validate_haiku_extended_thinking_budget():
    with pytest.raises(ValidationError, match="greater than or equal to 1024"):
        Settings(
            _env_file=None,
            llm_extended_thinking_budget_tokens=1023,
        )

    with pytest.raises(
        ValidationError,
        match=(
            "LLM_EXTENDED_THINKING_BUDGET_TOKENS must be less than "
            "LLM_MAX_TOKENS"
        ),
    ):
        Settings(
            _env_file=None,
            llm_model_tier="fast",
            llm_reasoning_enabled=True,
            llm_extended_thinking_budget_tokens=1024,
            llm_max_tokens=1024,
        )


def test_bedrock_bearer_token_is_redacted_from_settings_serialization():
    marker = "bedrock-secret-marker"
    settings = Settings(
        _env_file=None,
        aws_bearer_token_bedrock=marker,
    )

    assert marker not in repr(settings)
    assert marker not in repr(settings.model_dump())
    assert marker not in settings.model_dump_json()
    assert settings.aws_bearer_token_bedrock is not None
    assert settings.aws_bearer_token_bedrock.get_secret_value() == marker


def test_deterministic_test_provider_is_generation_ready():
    readiness = DeterministicTestProvider().generation_readiness()

    assert readiness.ok is True
    assert readiness.code == "OK"


def test_bedrock_generation_readiness_performs_and_caches_real_probe(
    monkeypatch,
):
    calls: list[dict] = []

    class FakeClient:
        def converse(self, **kwargs):
            calls.append(kwargs)
            return {"output": {"message": {"content": [{"text": "OK"}]}}}

    class FakeSession:
        def get_credentials(self):
            return object()

        def client(self, service_name, *, config):
            assert service_name == "bedrock-runtime"
            assert config.connect_timeout == 3.0
            return FakeClient()

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        model_id_for_tier=lambda _tier: "test-model",
    )

    first = provider.generation_readiness()
    second = provider.generation_readiness()

    assert first.ok is True
    assert first.code == "OK"
    assert second == first
    assert calls == [
        {
            "modelId": "test-model",
            "messages": [
                {
                    "role": "user",
                    "content": [{"text": "Reply with OK."}],
                }
            ],
            "inferenceConfig": {"maxTokens": 1},
        }
    ]


def test_bedrock_generation_readiness_fails_closed_without_credentials(
    monkeypatch,
):
    class FakeSession:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
    )

    readiness = provider.generation_readiness()

    assert readiness.ok is False
    assert readiness.code == "GENERATION_PROVIDER_CREDENTIALS_UNAVAILABLE"


def test_bedrock_generation_readiness_uses_secret_bearer_without_credential_lookup(
    monkeypatch,
):
    marker = "bedrock-secret-marker"

    class FakeClient:
        def converse(self, **_kwargs):
            assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == marker
            return {"output": {"message": {"content": [{"text": "OK"}]}}}

    class FakeSession:
        def get_credentials(self):
            raise AssertionError("bearer authentication must not query profile credentials")

        def client(self, service_name, *, config):
            assert service_name == "bedrock-runtime"
            return FakeClient()

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=SecretStr(marker),
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        model_id_for_tier=lambda _tier: "test-model",
    )

    readiness = provider.generation_readiness()

    assert readiness.ok is True
    assert readiness.code == "OK"


def test_bedrock_generation_readiness_rejects_malformed_success(
    monkeypatch,
):
    class FakeClient:
        def converse(self, **_kwargs):
            return {"output": {"message": {"content": []}}}

    class FakeSession:
        def get_credentials(self):
            return object()

        def client(self, _service_name, *, config):
            return FakeClient()

    monkeypatch.setattr(boto3, "Session", lambda **_kwargs: FakeSession())
    provider = BedrockAnthropicProvider()
    provider.settings = SimpleNamespace(
        aws_bearer_token_bedrock=None,
        aws_region="ap-northeast-2",
        aws_profile=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_session_token=None,
        model_id_for_tier=lambda _tier: "test-model",
    )

    readiness = provider.generation_readiness()

    assert readiness.ok is False
    assert readiness.code == "GENERATION_PROVIDER_RESPONSE_INVALID"
