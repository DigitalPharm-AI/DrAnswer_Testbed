from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


@dataclass(frozen=True)
class GenerationReadiness:
    ok: bool
    code: str


class BaseLLMProvider(ABC):
    @abstractmethod
    def chat_model(self) -> BaseChatModel:
        raise NotImplementedError

    def semantic_verification_model(self) -> BaseChatModel:
        """Return the low-latency model used for semantic equivalence checks."""

        return self.chat_model()

    def reference_linking_model(self) -> BaseChatModel:
        """Return the high-precision model for offline reference linking."""

        return self.semantic_verification_model()

    def generation_readiness(self) -> GenerationReadiness:
        """Fail closed unless a concrete provider proves generation is usable."""

        return GenerationReadiness(
            ok=False,
            code="GENERATION_PROVIDER_CHECK_UNSUPPORTED",
        )

    def configuration_readiness(self) -> GenerationReadiness:
        """Return a cost-free process-readiness result."""

        return GenerationReadiness(
            ok=False,
            code="GENERATION_PROVIDER_CONFIGURATION_UNSUPPORTED",
        )
