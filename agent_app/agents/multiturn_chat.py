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
from agent_app.generation import PROMPT_VERSION_ID, agent_error
from agent_app.prompt_builders import multiturn_chat_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.response_builders import natural_chat_summary, string_list
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse, MultiturnChatRequest


class MultiturnChatAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        agent_name = "system_event_agent"
        try:
            request = MultiturnChatRequest.model_validate(payload)
            request_payload = request.model_dump(mode="json")
            context = request.context if isinstance(request.context, dict) else {}
            async_tool_calls = context.get("async_tool_calls")
            catalog_tools = ToolCatalog.available_tools_payload()
            bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
            messages = build_chat_messages(
                multiturn_chat_prompt(),
                {
                    **request_payload,
                    "response_mode": "multiturn_chat",
                    "decision_type": "system_guidance",
                    "available_tools": catalog_tools,
                },
            )
            if context.get("execute_async_continuation") is True and isinstance(async_tool_calls, list):
                output = {"message": request.message, "observations": ["async_continuation"]}
                tool_calls = normalize_policy_tool_calls(
                    async_tool_calls,
                    source_event_type="multiturn_chat",
                )
                ai_message = ai_message_from_tool_calls(tool_calls, content=request.message, model_output=output)
            else:
                ai_message = await bound_model.ainvoke(messages)
                output = model_output_from_ai_message(ai_message)
                tool_calls = normalize_policy_tool_calls(tool_calls_from_ai_message(ai_message), source_event_type="multiturn_chat")
                ai_message = ai_message_from_tool_calls(tool_calls, content=str(ai_message.content or ""), model_output=output)
            continuation_type = async_continuation_type(tool_calls)
            if continuation_type and context.get("execute_async_continuation") is not True and not any(
                str(call.get("name") or "") == "mark_dose_taken" for call in tool_calls
            ):
                summary = async_continuation_summary(continuation_type)
                return AgentResponse(
                    trace_id=trace_id,
                    agent_name=agent_name,
                    prompt_version_id=PROMPT_VERSION_ID,
                    decision_type="async_continuation_requested",
                    structured_payload={
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
            executed_calls, results = await self.tool_runtime.execute(
                tool_calls,
                trace_id=trace_id,
                source_event_type="multiturn_chat",
                payload=request_payload,
                force_ae_after_positive_lookup=True,
            )
            executed_ai_message = ai_message_from_tool_calls(executed_calls, content=str(ai_message.content or ""), model_output=output) if executed_calls else ai_message
            tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
            final_ai_message = (
                await bound_model.ainvoke([*messages, executed_ai_message, *tool_messages])
                if executed_calls
                else ai_message
            )
            final_model_output = (
                model_output_from_ai_message(final_ai_message)
            )
        except Exception as exc:
            raise agent_error(trace_id, agent_name, "system_guidance", exc) from exc

        if executed_calls:
            decision_type = "side_effect_assessment" if any(call.get("name") in {"lookup_side_effect_info", "AE_pro_ctcae"} for call in executed_calls) else "tool_call"
            structured_payload = {
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
                agent_name="side_effect_triage_agent" if decision_type == "side_effect_assessment" else agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type=decision_type,
                structured_payload=structured_payload,
                human_summary=final_summary,
                requires_conversation_alert=False,
            )

        human_summary = patient_summary_from_ai_message(ai_message, natural_chat_summary(output))
        return AgentResponse(
            trace_id=trace_id,
            agent_name=agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="system_guidance",
            structured_payload={
                "observations": string_list(output.get("observations")),
                "model_output": output,
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
            },
            human_summary=human_summary,
            requires_conversation_alert=False,
        )
