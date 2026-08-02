from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmbeddingIdentity:
    provider: str
    model_id: str
    region: str
    dimensions: int


class EmbeddingProvider(Protocol):
    @property
    def identity(self) -> EmbeddingIdentity: ...

    async def embed_documents(
        self,
        texts: list[str],
    ) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...
