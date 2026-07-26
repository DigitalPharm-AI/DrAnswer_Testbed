from __future__ import annotations

from agent_app.providers.base import BaseLLMProvider
from agent_app.providers.bedrock import BedrockAnthropicProvider
from shared.settings import get_settings

RULE_BASED_PROVIDER_NAMES = {"rule_based", "rule-based", "local", "heuristic"}
RULE_BASED_ALLOWED_ENVS = {"test", "testing", "testbed"}


def create_llm_provider() -> BaseLLMProvider:
    settings = get_settings()
    provider_name = settings.llm_provider.strip().lower()
    if provider_name in RULE_BASED_PROVIDER_NAMES:
        if settings.app_env.strip().lower() not in RULE_BASED_ALLOWED_ENVS:
            raise RuntimeError(
                "RuleBasedProvider is restricted to APP_ENV=test, testing, or testbed."
            )
        from agent_app.providers.rule_based import RuleBasedProvider

        return RuleBasedProvider()
    if provider_name in {"bedrock", "bedrock_anthropic", "anthropic_bedrock"}:
        return BedrockAnthropicProvider()
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
