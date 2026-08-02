"""Embedding providers used by Agent-owned semantic reference searches."""

from agent_app.embeddings.base import EmbeddingIdentity, EmbeddingProvider
from agent_app.embeddings.cohere_bedrock import (
    BedrockCohereEmbeddingProvider,
    EmbeddingProviderError,
)

__all__ = [
    "BedrockCohereEmbeddingProvider",
    "EmbeddingIdentity",
    "EmbeddingProvider",
    "EmbeddingProviderError",
]
