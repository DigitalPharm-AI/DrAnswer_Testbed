from __future__ import annotations

from typing import Any, TypedDict

from agent_app.tools.policy_gate import ToolCallOrigin
from shared.schemas import AgentResponse, ToolCallResult


class MultiturnGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    context: dict[str, Any]
    messages: list[Any]
    bound_model: Any
    current_ai_message: Any
    current_model_output: dict[str, Any]
    current_final_text: str
    side_effect_request: bool
    side_effect_guard_checked: bool
    initial_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    continuation_type: str
    specialist_continuation_tool_calls: list[dict[str, Any]]
    seeded_tool_calls_consumed: bool
    seeded_tool_origin: ToolCallOrigin
    pending_tool_call_origin: ToolCallOrigin
    tool_dispatch_index: int
    round_executed_calls: list[dict[str, Any]]
    round_tool_results: list[ToolCallResult]
    round_delegation_calls: list[dict[str, Any]]
    round_delegated_responses: list[AgentResponse]
    round_direct_tool_calls: list[dict[str, Any]]
    round_direct_tool_results: list[ToolCallResult]
    round_confirmation_required: bool
    all_supervisor_tool_calls: list[dict[str, Any]]
    all_supervisor_tool_results: list[ToolCallResult]
    all_tool_messages: list[Any]
    delegation_calls: list[dict[str, Any]]
    delegated_responses: list[AgentResponse]
    direct_tool_calls: list[dict[str, Any]]
    direct_tool_results: list[ToolCallResult]
    tool_round_result_counts: list[int]
    iterations: int
    confirmation_required: bool
    confirmation_ai_message: Any
    confirmation_model_output: dict[str, Any]
    entry_mode: str
    mutation_resolution_llm_timings_ms: list[int]
    confirmation_reply_ai_message: Any
    confirmation_reply_model_output: dict[str, Any]
    confirmation_reply_intent: str
    confirmation_reply_llm_elapsed_ms: int
    response: AgentResponse
