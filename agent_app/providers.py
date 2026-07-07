from __future__ import annotations

from agent_app.bedrock_provider import BedrockAnthropicProvider
from agent_app.provider_base import BaseLLMProvider
from agent_app.provider_config import describe_model_config, get_runtime_model_tier, set_runtime_model_tier
from agent_app.provider_json import parse_json_object, strip_json_fence
from agent_app.rule_based_provider import RuleBasedProvider
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


__all__ = [
    "BaseLLMProvider",
    "BedrockAnthropicProvider",
    "RuleBasedProvider",
    "create_llm_provider",
    "describe_model_config",
    "get_runtime_model_tier",
    "parse_json_object",
    "set_runtime_model_tier",
    "strip_json_fence",
]
