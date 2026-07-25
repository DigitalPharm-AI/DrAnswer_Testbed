from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    human_payload_from_messages,
    langchain_tool_name,
    system_prompt_from_messages,
    tool_results_from_messages,
)
from agent_app.llm.responses import natural_chat_summary
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.calling import normalize_tool_calls
from agent_app.tools.results import tool_result_summary


class NativeChatProvider(BaseLLMProvider):
    def chat_model(self):
        return NativeProviderChatModel(provider=self)

    async def model_output(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class NativeProviderChatModel(BaseChatModel):
    provider: Any
    bound_tools: list[dict[str, Any]] = Field(default_factory=list)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @property
    def _llm_type(self) -> str:
        return "native_test_chat_model"

    def bind_tools(self, tools, *, tool_choice: str | None = None, **kwargs):
        self.provider.bound_tool_names = [langchain_tool_name(tool) for tool in tools]
        return self.model_copy(update={"bound_tools": list(tools)})

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        return asyncio.run(self._agenerate(messages, stop=stop, run_manager=None, **kwargs))

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager=None,
        **kwargs: Any,
    ) -> ChatResult:
        history = getattr(self.provider, "chat_model_bound_tool_history", [])
        history.append([langchain_tool_name(tool) for tool in self.bound_tools])
        self.provider.chat_model_bound_tool_history = history

        payload = human_payload_from_messages(messages)
        tool_results = tool_results_from_messages(messages)
        if tool_results:
            finalizer = getattr(self.provider, "finalize_tool_results", None)
            if callable(finalizer):
                output = await finalizer(system_prompt_from_messages(messages), payload, tool_results)
                if not isinstance(output, dict):
                    output = {}
                next_tool_calls = normalize_tool_calls(output)
                if next_tool_calls:
                    return _chat_result(
                        ai_message_from_tool_calls(
                            next_tool_calls,
                            content=natural_chat_summary(output),
                            model_output=output,
                        )
                    )
                return _chat_result(
                    AIMessage(
                        content=natural_chat_summary(output),
                        response_metadata={"model_output": output},
                    )
                )
            fallback = tool_result_summary(tool_results, "도구 실행 결과를 확인했습니다.")
            return _chat_result(
                AIMessage(
                    content=fallback,
                    response_metadata={
                        "model_output": {
                            "message": fallback,
                            "fallback": "tool_result_summary",
                        }
                    },
                )
            )

        output = await self.provider.model_output(system_prompt_from_messages(messages), payload)
        if not isinstance(output, dict):
            output = {}
        tool_calls = normalize_tool_calls(output)
        if tool_calls:
            return _chat_result(
                ai_message_from_tool_calls(
                    tool_calls,
                    content=natural_chat_summary(output),
                    model_output=output,
                )
            )
        return _chat_result(
            AIMessage(
                content=natural_chat_summary(output),
                response_metadata={"model_output": output},
            )
        )


def _chat_result(message: AIMessage) -> ChatResult:
    return ChatResult(generations=[ChatGeneration(message=message)])
