from __future__ import annotations

from typing import Any

from agent_app.agent_delegation import (
    delegation_reason,
    delegation_target,
    delegation_tools_payload,
    is_delegation_tool_call,
)
from agent_app.agents.medication import MedicationAgent
from agent_app.agents.nutrition_management import NutritionManagementAgent
from agent_app.agents.nutrition_recommendation import NutritionRecommendationAgent
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
        self.medication_agent = MedicationAgent(provider, tool_runtime)
        self.nutrition_management_agent = NutritionManagementAgent(provider, tool_runtime)
        self.nutrition_recommendation_agent = NutritionRecommendationAgent(provider, tool_runtime)

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        agent_name = "system_event_agent"
        try:
            request = MultiturnChatRequest.model_validate(payload)
            request_payload = request.model_dump(mode="json")
            context = request.context if isinstance(request.context, dict) else {}
            async_tool_calls = context.get("async_tool_calls")
            catalog_tools = [*ToolCatalog.available_tools_payload(), *delegation_tools_payload()]
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
            delegated_call = next((call for call in tool_calls if is_delegation_tool_call(call)), None)
            if delegated_call is not None:
                delegated_response = await self._run_delegation(trace_id, request_payload, delegated_call)
                return self._delegated_response(delegated_response, delegated_call=delegated_call, reason=delegation_reason(delegated_call))
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
                        "routing_mode": "direct_async_continuation",
                        "supervisor_agent": agent_name,
                        "executed_by": agent_name,
                        "model_output": output,
                        "tool_calls": tool_calls,
                        "supervisor_tool_calls": tool_calls,
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
                routing_context={
                    "routing_mode": "direct_async_continuation" if context.get("execute_async_continuation") is True else "direct_tool",
                    "executed_by": agent_name,
                    "supervisor_agent": agent_name,
                    "supervisor_tool_names": [str(call.get("name") or "") for call in tool_calls],
                    "tool_names": [str(call.get("name") or "") for call in tool_calls],
                },
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
                "routing_mode": "direct_tool",
                "supervisor_agent": agent_name,
                "executed_by": agent_name,
                "supervisor_tool_calls": executed_calls,
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
                "routing_mode": "direct_answer",
                "supervisor_agent": agent_name,
                "executed_by": agent_name,
                "supervisor_tool_calls": [],
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

    async def _run_delegation(self, trace_id: str, request_payload: dict[str, Any], tool_call: dict[str, Any]) -> AgentResponse:
        target = delegation_target(tool_call)
        if target == "medication_agent":
            return await self.medication_agent.run(trace_id, request_payload)
        if target == "nutrition_management_agent":
            return await self.nutrition_management_agent.run(trace_id, request_payload)
        if target == "nutrition_recommendation_agent":
            return await self.nutrition_recommendation_agent.run(trace_id, request_payload)
        raise ValueError(f"unsupported_delegation_target:{target or 'unknown'}")

    @staticmethod
    def _delegated_response(delegated_response: AgentResponse, *, delegated_call: dict[str, Any], reason: str = "") -> AgentResponse:
        structured = dict(delegated_response.structured_payload)
        specialist_tool_calls = structured.get("tool_calls") if isinstance(structured.get("tool_calls"), list) else []
        structured["routing_mode"] = "delegated_agent"
        structured["supervisor_agent"] = "system_event_agent"
        structured["specialist_agent"] = delegated_response.agent_name
        structured["executed_by"] = delegated_response.agent_name
        structured["delegated_agent"] = delegated_response.agent_name
        structured["delegation_reason"] = reason
        structured["delegated_by"] = "system_event_agent"
        structured["supervisor_tool_calls"] = [delegated_call]
        structured["specialist_tool_calls"] = specialist_tool_calls
        return AgentResponse(
            trace_id=delegated_response.trace_id,
            agent_name=delegated_response.agent_name,
            prompt_version_id=delegated_response.prompt_version_id,
            decision_type=delegated_response.decision_type,
            structured_payload=structured,
            human_summary=delegated_response.human_summary,
            requires_conversation_alert=delegated_response.requires_conversation_alert,
            validation_passed=delegated_response.validation_passed,
            validation_errors=delegated_response.validation_errors,
        )
