from __future__ import annotations

from functools import lru_cache

from agent_app.embeddings.base import EmbeddingProvider
from agent_app.embeddings.cohere_bedrock import (
    BedrockCohereEmbeddingProvider,
)
from shared.settings import get_settings


@lru_cache(maxsize=1)
def get_embedding_provider() -> EmbeddingProvider:
    settings = get_settings()
    if settings.embedding_provider.strip() == "bedrock_cohere":
        return BedrockCohereEmbeddingProvider(settings)
    raise RuntimeError(
        f"unsupported_embedding_provider:{settings.embedding_provider}"
    )
