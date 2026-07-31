from __future__ import annotations

from agent_app.providers.base import BaseLLMProvider
from agent_app.providers.bedrock import BedrockAnthropicProvider
from shared.settings import get_settings

DETERMINISTIC_TEST_PROVIDER_NAMES = {
    "deterministic_test",
    "deterministic-test",
}
DETERMINISTIC_TEST_ALLOWED_ENVS = {"test", "testing", "testbed"}


def create_llm_provider() -> BaseLLMProvider:
    settings = get_settings()
    provider_name = settings.llm_provider.strip().lower()
    if provider_name in DETERMINISTIC_TEST_PROVIDER_NAMES:
        if settings.app_env.strip().lower() not in DETERMINISTIC_TEST_ALLOWED_ENVS:
            raise RuntimeError(
                "DeterministicTestProvider is restricted to APP_ENV=test, "
                "testing, or testbed."
            )
        from agent_app.providers.deterministic_test import (
            DeterministicTestProvider,
        )

        return DeterministicTestProvider()
    if provider_name in {"bedrock", "bedrock_anthropic", "anthropic_bedrock"}:
        return BedrockAnthropicProvider()
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
