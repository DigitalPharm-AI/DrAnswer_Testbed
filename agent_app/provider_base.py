from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def chat_model(self) -> BaseChatModel:
        raise NotImplementedError(f"{type(self).__name__} does not expose a LangChain ChatModel.")
