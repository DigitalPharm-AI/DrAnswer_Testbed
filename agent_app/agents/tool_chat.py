from __future__ import annotations

from typing import Any

from agent_app.chat_tooling import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    patient_summary_from_ai_message,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.continuation_policy import async_continuation_summary, async_continuation_type
from agent_app.generation import PROMPT_VERSION_ID
from agent_app.providers import BaseLLMProvider
from agent_app.response_builders import natural_chat_summary, string_list
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse


async def run_tool_chat_agent(
    *,
    provider: BaseLLMProvider,
    tool_runtime: ToolRuntime,
    trace_id: str,
    request_payload: dict[str, Any],
    agent_name: str,
    prompt: str,
    response_mode: str,
    decision_type: str,
    tool_names: tuple[str, ...],
    source_event_type: str,
    forced_tool_calls: list[dict[str, Any]] | None = None,
    force_ae_after_positive_lookup: bool = False,
) -> AgentResponse:
    catalog_tools = ToolCatalog.tools_for(*tool_names)
    bound_model = provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
    messages = build_chat_messages(
        prompt,
        {
            **request_payload,
            "response_mode": response_mode,
            "decision_type": decision_type,
            "available_tools": catalog_tools,
        },
    )
    if forced_tool_calls is not None:
        output = {"message": request_payload.get("message"), "observations": ["forced_tool_calls"]}
        tool_calls = normalize_policy_tool_calls(forced_tool_calls, source_event_type=source_event_type)
        ai_message = ai_message_from_tool_calls(tool_calls, content=str(request_payload.get("message") or ""), model_output=output)
    else:
        ai_message = await bound_model.ainvoke(messages)
        output = model_output_from_ai_message(ai_message)
        tool_calls = normalize_policy_tool_calls(tool_calls_from_ai_message(ai_message), source_event_type=source_event_type)
        ai_message = ai_message_from_tool_calls(tool_calls, content=str(ai_message.content or ""), model_output=output)

    continuation_type = async_continuation_type(tool_calls)
    if continuation_type and forced_tool_calls is None and not any(str(call.get("name") or "") == "mark_dose_taken" for call in tool_calls):
        summary = async_continuation_summary(continuation_type)
        return AgentResponse(
            trace_id=trace_id,
            agent_name=agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="async_continuation_requested",
            structured_payload={
                "routing_mode": "specialist_async_continuation",
                "executed_by": agent_name,
                "specialist_agent": agent_name,
                "specialist_tool_calls": tool_calls,
                "model_output": output,
                "tool_calls": tool_calls,
                "tool_results": [],
                "tools_executed": False,
                "message_flow": ["HumanMessage", "AIMessage(tool_calls)"],
                "async_continuation_required": True,
                "async_continuation_type": continuation_type,
                "policy_confirmation_required": has_deferred_policy_tool_call(tool_calls),
            },
            human_summary=summary,
            requires_conversation_alert=False,
        )

    executed_calls, results = await tool_runtime.execute(
        tool_calls,
        trace_id=trace_id,
        source_event_type=source_event_type,
        payload=request_payload,
        force_ae_after_positive_lookup=force_ae_after_positive_lookup,
        routing_context={
            "routing_mode": "specialist_tool",
            "executed_by": agent_name,
            "specialist_agent": agent_name,
            "specialist_tool_names": [str(call.get("name") or "") for call in tool_calls],
            "tool_names": [str(call.get("name") or "") for call in tool_calls],
        },
    )
    executed_ai_message = ai_message_from_tool_calls(executed_calls, content=str(ai_message.content or ""), model_output=output) if executed_calls else ai_message
    tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
    final_ai_message = await bound_model.ainvoke([*messages, executed_ai_message, *tool_messages]) if executed_calls else ai_message
    final_model_output = model_output_from_ai_message(final_ai_message)

    if executed_calls:
        structured_payload = {
            "routing_mode": "specialist_tool",
            "executed_by": agent_name,
            "specialist_agent": agent_name,
            "specialist_tool_calls": executed_calls,
            "model_output": output,
            **tool_calls_payload(executed_calls, results),
            "tool_messages": [
                {
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
                for message in tool_messages
            ],
            "final_model_output": final_model_output,
            "message_flow": ["HumanMessage", "AIMessage(tool_calls)", "ToolMessage", "AIMessage(final_answer)"],
            "policy_confirmation_required": has_deferred_policy_tool_call(executed_calls),
        }
        fallback_summary = tool_result_summary(results, natural_chat_summary(output) or "도구를 실행했습니다.")
        final_summary = patient_summary_from_ai_message(final_ai_message, fallback_summary)
        return AgentResponse(
            trace_id=trace_id,
            agent_name=agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type=decision_type,
            structured_payload=structured_payload,
            human_summary=final_summary,
            requires_conversation_alert=False,
        )

    return AgentResponse(
        trace_id=trace_id,
        agent_name=agent_name,
        prompt_version_id=PROMPT_VERSION_ID,
        decision_type=decision_type,
        structured_payload={
            "routing_mode": "specialist_answer",
            "executed_by": agent_name,
            "specialist_agent": agent_name,
            "specialist_tool_calls": [],
            "observations": string_list(output.get("observations")),
            "model_output": output,
            "tool_calls": [],
            "tool_results": [],
            "tools_executed": False,
            "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
        },
        human_summary=patient_summary_from_ai_message(ai_message, natural_chat_summary(output)),
        requires_conversation_alert=False,
    )
