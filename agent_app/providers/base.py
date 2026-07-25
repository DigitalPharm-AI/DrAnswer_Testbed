from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel


class BaseLLMProvider(ABC):
    @abstractmethod
    def chat_model(self) -> BaseChatModel:
        raise NotImplementedError
