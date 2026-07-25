from __future__ import annotations

from agent_app.providers.base import BaseLLMProvider
from agent_app.providers.bedrock import BedrockAnthropicProvider
from shared.settings import get_settings

RULE_BASED_PROVIDER_NAMES = {"rule_based", "rule-based", "local", "heuristic"}


def create_llm_provider() -> BaseLLMProvider:
    settings = get_settings()
    provider_name = settings.llm_provider.strip().lower()
    if provider_name in RULE_BASED_PROVIDER_NAMES:
        raise RuntimeError("RuleBasedProvider is test-only and cannot be selected through LLM_PROVIDER at runtime.")
    if provider_name in {"bedrock", "bedrock_anthropic", "anthropic_bedrock"}:
        return BedrockAnthropicProvider()
    raise RuntimeError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
