from __future__ import annotations

from typing import Any

from agent_app.agents.tool_chat import ToolChatAgentGraph
from agent_app.errors import AgentExecutionError
from agent_app.llm.prompts import medication_agent_prompt
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.policy_gate import ToolCallOrigin
from agent_app.tools.runtime import ToolRuntime
from agent_app.tools.side_effects import is_medication_side_effect_feature_call
from shared.schemas import AgentResponse
from shared.tool_names import (
    MEDICATION_AGENT_CALLABLE_TOOLS,
    MEDICATION_SIDE_EFFECT_FEATURE_TOOLS,
    SOURCE_MEDICATION_AGENT,
)


class MedicationAgent:
    def __init__(
        self,
        provider: BaseLLMProvider,
        tool_runtime: ToolRuntime,
        *,
        medication_side_effect_enabled: bool = True,
    ) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.medication_side_effect_enabled = medication_side_effect_enabled
        callable_tools = set(MEDICATION_AGENT_CALLABLE_TOOLS)
        if not medication_side_effect_enabled:
            callable_tools -= MEDICATION_SIDE_EFFECT_FEATURE_TOOLS
        self.graph_runner = ToolChatAgentGraph(
            provider=self.provider,
            tool_runtime=self.tool_runtime,
            agent_name="medication_agent",
            prompt=medication_agent_prompt(
                medication_side_effect_enabled=medication_side_effect_enabled
            ),
            response_mode="medication_chat",
            decision_type="tool_call",
            tool_names=tuple(sorted(callable_tools)),
            source_event_type=SOURCE_MEDICATION_AGENT,
            force_ae_after_positive_lookup=medication_side_effect_enabled,
        )

    async def run(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
    ) -> AgentResponse:
        return await self.graph_runner.invoke(trace_id, request_payload)

    async def continue_with_tool_calls(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_calls: list[dict[str, Any]],
        origin: ToolCallOrigin = ToolCallOrigin.CLINICAL_CONTINUATION,
    ) -> AgentResponse:
        self._validate_side_effect_tool_calls(trace_id, tool_calls)
        return await self.graph_runner.continue_with_tool_calls(
            trace_id,
            request_payload,
            tool_calls=tool_calls,
            origin=origin,
        )

    async def execute_approved_write(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_call: dict[str, Any],
    ) -> AgentResponse:
        self._validate_side_effect_tool_calls(trace_id, [tool_call])
        return await self.graph_runner.execute_approved_write(
            trace_id,
            request_payload,
            tool_call=tool_call,
        )
    def _validate_side_effect_tool_calls(
        self,
        trace_id: str,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        if self.medication_side_effect_enabled:
            return
        if any(
            is_medication_side_effect_feature_call(tool_call)
            for tool_call in tool_calls
        ):
            raise AgentExecutionError(
                "medication_side_effect_feature_disabled",
                error_type="disabled_feature_tool_call",
                trace_id=trace_id,
                agent_name="medication_agent",
                decision_type="tool_call",
                retryable=False,
            )
