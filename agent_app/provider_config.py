from __future__ import annotations

from threading import Lock

from shared.schemas import AgentModelConfig
from shared.settings import get_settings, normalize_model_tier

_model_tier_lock = Lock()
_runtime_model_tier = normalize_model_tier(get_settings().llm_model_tier)


def get_runtime_model_tier() -> str:
    with _model_tier_lock:
        return _runtime_model_tier


def set_runtime_model_tier(model_tier: str) -> AgentModelConfig:
    global _runtime_model_tier
    normalized_tier = normalize_model_tier(model_tier)
    with _model_tier_lock:
        _runtime_model_tier = normalized_tier
    return describe_model_config()


def describe_model_config() -> AgentModelConfig:
    settings = get_settings()
    model_tier = get_runtime_model_tier()
    return AgentModelConfig(
        provider=settings.llm_provider,
        model_tier=model_tier,
        model_id=settings.model_id_for_tier(model_tier),
        available_tiers=settings.available_model_tiers(),
    )
