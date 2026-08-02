from __future__ import annotations

from typing import Any

from agent_app.agents.multiturn_output import MULTITURN_CHAT_AGENT_NAME
from agent_app.agents.tool_chat import TOOL_LOOP_MODE
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID
from agent_app.llm.messages import (
    build_chat_messages,
    public_text_from_ai_message,
    tool_calls_from_ai_message,
)
from agent_app.llm.prompts import multiturn_chat_prompt
from agent_app.observability.model_calls import traced_model_astream_message
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.policy import normalize_policy_tool_calls
from agent_app.tools.policy_gate import ToolCallContext, ToolCallOrigin
from agent_app.tools.results import public_tool_calls, tool_calls_payload
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse


async def execute_approved_supervisor_write(
    provider: BaseLLMProvider,
    tool_runtime: ToolRuntime,
    trace_id: str,
    request_payload: dict[str, Any],
    tool_call: dict[str, Any],
) -> AgentResponse:
    executed_calls, results = await tool_runtime.execute(
        [tool_call],
        trace_id=trace_id,
        source_event_type="multiturn_chat",
        payload=request_payload,
        call_context=ToolCallContext(origin=ToolCallOrigin.APPROVED_WRITE),
        routing_context={
            "routing_mode": "approved_write",
            "executed_by": MULTITURN_CHAT_AGENT_NAME,
            "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
            "tool_loop_mode": "direct_approved_write",
            "agent_graph_mode": TOOL_LOOP_MODE,
            "tool_names": [str(tool_call.get("name") or "")],
        },
    )
    if len(executed_calls) != 1 or len(results) != 1:
        raise AgentExecutionError(
            "approved_write_execution_result_count_mismatch",
            error_type="mutation_tool_not_applied",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
        )
    result = results[0]
    if result.status != "success":
        raise AgentExecutionError(
            f"approved_write_not_applied:{result.tool_name}:{result.status}",
            error_type="mutation_tool_not_applied",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
            retryable=result.retryable,
        )

    finalizer_messages = build_chat_messages(
        multiturn_chat_prompt(),
        {
            **request_payload,
            "response_mode": "approved_write_finalization",
            "decision_type": "system_guidance",
            "approved_write_result": result.model_dump(mode="json"),
        },
    )
    ai_message = await traced_model_astream_message(
        provider.chat_model(),
        finalizer_messages,
        name="multiturn_chat.approved_write_finalizer",
        prompt_version_id=PROMPT_VERSION_ID,
        publish_public_text=True,
    )
    followup_calls = normalize_policy_tool_calls(
        tool_calls_from_ai_message(ai_message),
        source_event_type="multiturn_chat",
    )
    if followup_calls:
        raise AgentExecutionError(
            "approved_write_followup_tool_forbidden",
            error_type="approved_write_followup_tool_forbidden",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
        )
    final_text = public_text_from_ai_message(ai_message)
    if not final_text:
        raise AgentExecutionError(
            "llm_final_answer_missing",
            error_type="llm_final_answer_missing",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type="system_guidance",
        )
    return AgentResponse(
        trace_id=trace_id,
        agent_name=MULTITURN_CHAT_AGENT_NAME,
        prompt_version_id=PROMPT_VERSION_ID,
        decision_type="system_guidance",
        structured_payload={
            "routing_mode": "approved_write",
            "tool_loop_mode": "direct_approved_write",
            "agent_graph_mode": TOOL_LOOP_MODE,
            "executed_by": MULTITURN_CHAT_AGENT_NAME,
            "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
            "supervisor_tool_calls": public_tool_calls(executed_calls),
            **tool_calls_payload(executed_calls, results),
            "final_answer_source": "approved_write_finalizer",
            "message_flow": [
                "ApprovedToolCall",
                "ToolResult",
                "AIMessage(final_answer)",
            ],
        },
        human_summary=final_text,
        requires_conversation_alert=False,
    )
