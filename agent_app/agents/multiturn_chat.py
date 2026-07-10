from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent_app import trace_logging
from agent_app.agent_delegation import (
    delegation_reason,
    delegation_target,
    delegation_tools_payload,
    is_delegation_tool_call,
)
from agent_app.agents.medication import MedicationAgent
from agent_app.agents.nutrition_management import NutritionManagementAgent
from agent_app.agents.nutrition_recommendation import NutritionRecommendationAgent
from agent_app.agents.single_round_graph import (
    AGENT_GRAPH_MODE,
    LLM_AFTER_TOOL_FINALIZATION_MODE,
    SINGLE_ROUND_TOOL_EXECUTION_MODE,
)
from agent_app.chat_tooling import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    patient_summary_with_source,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.continuation_policy import async_continuation_summary, async_continuation_type
from agent_app.errors import AgentExecutionError
from agent_app.generation import PROMPT_VERSION_ID, agent_error
from agent_app.output_validation import validate_llm_output
from agent_app.prompt_builders import multiturn_chat_prompt
from agent_app.providers import BaseLLMProvider
from agent_app.response_builders import finalized_chat_summary, natural_chat_summary, string_list
from agent_app.tool_catalog import ToolCatalog
from agent_app.tool_names import DELEGATE_TO_MEDICATION_AGENT, SIDE_EFFECT_TOOLS
from agent_app.tool_permissions import POLICY_TOOLS
from agent_app.tool_policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tool_results import tool_calls_payload, tool_result_summary
from agent_app.tool_runtime import ToolRuntime
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse, MultiturnChatRequest, ToolCallResult

SUPERVISOR_DIRECT_TOOLS = tuple(sorted(POLICY_TOOLS))
SUPERVISOR_AGENT_NAME = "system_event_agent"


class MultiturnGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    context: dict[str, Any]
    messages: list[Any]
    decision_ai_message: Any
    decision_model_output: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    continuation_type: str
    delegated_call: dict[str, Any]
    forced_specialist_tool_calls: list[dict[str, Any]]
    delegated_response: AgentResponse
    delegated_tool_result: ToolCallResult
    executed_calls: list[dict[str, Any]]
    tool_results: list[ToolCallResult]
    tool_messages: list[Any]
    final_ai_message: Any
    final_model_output: dict[str, Any]
    completion_kind: str
    response: AgentResponse


class MultiturnChatAgent:
    def __init__(self, provider: BaseLLMProvider, tool_runtime: ToolRuntime) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.medication_agent = MedicationAgent(provider, tool_runtime)
        self.nutrition_management_agent = NutritionManagementAgent(provider, tool_runtime)
        self.nutrition_recommendation_agent = NutritionRecommendationAgent(provider, tool_runtime)
        self.graph = self._build_graph()

    async def run(self, trace_id: str, payload: dict[str, Any]) -> AgentResponse:
        try:
            request = MultiturnChatRequest.model_validate(payload)
            request_payload = request.model_dump(mode="json")
            context = request.context if isinstance(request.context, dict) else {}
            final_state = await self.graph.ainvoke(
                {
                    "trace_id": trace_id,
                    "request_payload": request_payload,
                    "context": context,
                    "executed_calls": [],
                    "tool_results": [],
                    "tool_messages": [],
                    "forced_specialist_tool_calls": [],
                }
            )
            return final_state["response"]
        except Exception as exc:
            raise agent_error(trace_id, SUPERVISOR_AGENT_NAME, "system_guidance", exc) from exc

    def _build_graph(self):
        graph = StateGraph(MultiturnGraphState)
        graph.add_node("supervisor_decision", self._supervisor_decision)
        graph.add_node("delegation", self._delegation_node)
        graph.add_node("direct_tool", self._direct_tool_node)
        graph.add_node("supervisor_finalization", self._supervisor_finalization)
        graph.add_node("direct_answer_response", self._direct_answer_response)
        graph.add_node("async_continuation_response", self._async_continuation_response)
        graph.add_node("direct_tool_response", self._direct_tool_response)
        graph.add_node("delegated_response", self._delegated_response)
        graph.add_edge(START, "supervisor_decision")
        graph.add_conditional_edges(
            "supervisor_decision",
            self._route_after_decision,
            {
                "delegation": "delegation",
                "async_continuation_response": "async_continuation_response",
                "direct_tool": "direct_tool",
                "direct_answer_response": "direct_answer_response",
            },
        )
        graph.add_edge("delegation", "supervisor_finalization")
        graph.add_edge("direct_tool", "supervisor_finalization")
        graph.add_conditional_edges(
            "supervisor_finalization",
            self._route_after_finalization,
            {
                "delegated_response": "delegated_response",
                "direct_tool_response": "direct_tool_response",
            },
        )
        graph.add_edge("direct_answer_response", END)
        graph.add_edge("async_continuation_response", END)
        graph.add_edge("direct_tool_response", END)
        graph.add_edge("delegated_response", END)
        return graph.compile()

    async def _supervisor_decision(self, state: MultiturnGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        context = state.get("context", {})
        catalog_tools = [*ToolCatalog.tools_for(*SUPERVISOR_DIRECT_TOOLS), *delegation_tools_payload()]
        messages = build_chat_messages(
            multiturn_chat_prompt(),
            {
                **request_payload,
                "response_mode": "multiturn_chat",
                "decision_type": "system_guidance",
                "available_tools": catalog_tools,
            },
        )
        async_tool_calls = context.get("async_tool_calls")
        forced_specialist_tool_calls: list[dict[str, Any]] = []
        delegated_call: dict[str, Any] | None = None

        if context.get("execute_async_continuation") is True and isinstance(async_tool_calls, list):
            output = {"message": request_payload.get("message"), "observations": ["async_continuation"]}
            tool_calls = normalize_policy_tool_calls(async_tool_calls, source_event_type="multiturn_chat")
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(request_payload.get("message") or ""),
                model_output=output,
            )
            if any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in tool_calls):
                delegated_call = {
                    "name": DELEGATE_TO_MEDICATION_AGENT,
                    "arguments": {
                        "task": "Continue the deferred medication side-effect assessment.",
                        "reason": "async_medication_continuation",
                    },
                }
                forced_specialist_tool_calls = tool_calls
                ai_message = ai_message_from_tool_calls(
                    [delegated_call],
                    content=str(request_payload.get("message") or ""),
                    model_output=output,
                )
        else:
            bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
            ai_message = await bound_model.ainvoke(messages)
            output = model_output_from_ai_message(ai_message)
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type="multiturn_chat",
            )
            validation_output = _model_output_with_tool_calls(output, tool_calls)
            self._validate_output(state["trace_id"], validation_output, request_payload)
            output = validation_output
            if tool_calls:
                ai_message = ai_message_from_tool_calls(
                    tool_calls,
                    content=str(ai_message.content or ""),
                    model_output=output,
                )
            delegated_call = next((call for call in tool_calls if is_delegation_tool_call(call)), None)

        return {
            "messages": messages,
            "decision_ai_message": ai_message,
            "decision_model_output": output,
            "tool_calls": tool_calls,
            "continuation_type": async_continuation_type(tool_calls),
            "delegated_call": delegated_call,
            "forced_specialist_tool_calls": forced_specialist_tool_calls,
        }

    @staticmethod
    def _route_after_decision(state: MultiturnGraphState) -> str:
        if state.get("delegated_call"):
            return "delegation"
        continuation_type = state.get("continuation_type", "")
        if continuation_type and state.get("context", {}).get("execute_async_continuation") is not True:
            return "async_continuation_response"
        if state.get("tool_calls"):
            return "direct_tool"
        return "direct_answer_response"

    async def _delegation_node(self, state: MultiturnGraphState) -> dict[str, Any]:
        delegated_call = state["delegated_call"]
        target = delegation_target(delegated_call)
        forced_tool_calls = state.get("forced_specialist_tool_calls", [])
        if target == "medication_agent":
            delegated_response = await self.medication_agent.run(
                state["trace_id"],
                state["request_payload"],
                forced_tool_calls=forced_tool_calls or None,
            )
        elif target == "nutrition_management_agent":
            delegated_response = await self.nutrition_management_agent.run(state["trace_id"], state["request_payload"])
        elif target == "nutrition_recommendation_agent":
            delegated_response = await self.nutrition_recommendation_agent.run(state["trace_id"], state["request_payload"])
        else:
            raise ValueError(f"unsupported_delegation_target:{target or 'unknown'}")
        delegated_tool_result = ToolCallResult(
            tool_name=str(delegated_call.get("name") or "delegated_agent"),
            status="success",
            response={
                "specialist_agent": delegated_response.agent_name,
                "decision_type": delegated_response.decision_type,
                "human_summary": delegated_response.human_summary,
                "structured_payload": delegated_response.structured_payload,
            },
            idempotency_key=f"{delegated_response.trace_id}:{delegated_call.get('name', 'delegated_agent')}:delegation",
        )
        return {
            "delegated_response": delegated_response,
            "delegated_tool_result": delegated_tool_result,
            "completion_kind": "delegation",
        }

    async def _direct_tool_node(self, state: MultiturnGraphState) -> dict[str, Any]:
        tool_calls = state.get("tool_calls", [])
        ai_message = state["decision_ai_message"]
        output = state.get("decision_model_output", {})
        context = state.get("context", {})
        executed_calls, results = await self.tool_runtime.execute(
            tool_calls,
            trace_id=state["trace_id"],
            source_event_type="multiturn_chat",
            payload=state["request_payload"],
            routing_context={
                "routing_mode": "direct_async_continuation" if context.get("execute_async_continuation") is True else "direct_tool",
                "executed_by": SUPERVISOR_AGENT_NAME,
                "supervisor_agent": SUPERVISOR_AGENT_NAME,
                "agent_graph_mode": AGENT_GRAPH_MODE,
                "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
                "supervisor_tool_names": [str(call.get("name") or "") for call in tool_calls],
                "tool_names": [str(call.get("name") or "") for call in tool_calls],
            },
        )
        if len(results) != len(executed_calls):
            raise RuntimeError("supervisor_tool_execution_result_count_mismatch")
        executed_ai_message = ai_message_from_tool_calls(
            executed_calls,
            content=str(ai_message.content or ""),
            model_output=output,
        )
        tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
        return {
            "messages": [*state["messages"], executed_ai_message, *tool_messages],
            "executed_calls": executed_calls,
            "tool_results": results,
            "tool_messages": tool_messages,
            "completion_kind": "direct_tool",
        }

    async def _supervisor_finalization(self, state: MultiturnGraphState) -> dict[str, Any]:
        if state.get("completion_kind") == "delegation":
            delegated_call = state["delegated_call"]
            executed_ai_message = ai_message_from_tool_calls(
                [delegated_call],
                content=str(state["decision_ai_message"].content or ""),
                model_output=state.get("decision_model_output", {}),
            )
            tool_messages = tool_messages_from_results(
                executed_ai_message,
                [delegated_call],
                [state["delegated_tool_result"]],
            )
            final_messages = [*state["messages"], executed_ai_message, *tool_messages]
        else:
            tool_messages = state.get("tool_messages", [])
            final_messages = state["messages"]
        final_ai_message = await self.provider.chat_model().ainvoke(final_messages)
        if tool_calls_from_ai_message(final_ai_message):
            raise RuntimeError("supervisor_finalizer_returned_tool_calls")
        final_model_output = model_output_from_ai_message(final_ai_message)
        self._validate_output(state["trace_id"], final_model_output, state["request_payload"])
        return {
            "tool_messages": tool_messages,
            "final_ai_message": final_ai_message,
            "final_model_output": final_model_output,
        }

    @staticmethod
    def _route_after_finalization(state: MultiturnGraphState) -> str:
        return "delegated_response" if state.get("completion_kind") == "delegation" else "direct_tool_response"

    @staticmethod
    def _direct_answer_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        output = state.get("decision_model_output", {})
        ai_message = state["decision_ai_message"]
        human_summary, final_answer_source = patient_summary_with_source(
            ai_message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=SUPERVISOR_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="system_guidance",
                structured_payload={
                    "routing_mode": "direct_answer",
                    "supervisor_agent": SUPERVISOR_AGENT_NAME,
                    "executed_by": SUPERVISOR_AGENT_NAME,
                    "supervisor_tool_calls": [],
                    "observations": string_list(output.get("observations")),
                    "model_output": output,
                    "tool_calls": [],
                    "tool_results": [],
                    "tools_executed": False,
                    "final_answer_source": final_answer_source,
                    "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
                    "agent_graph_mode": AGENT_GRAPH_MODE,
                    "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
                },
                human_summary=human_summary,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _async_continuation_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        continuation_type = state.get("continuation_type", "")
        tool_calls = state.get("tool_calls", [])
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=SUPERVISOR_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="async_continuation_requested",
                structured_payload={
                    "routing_mode": "direct_async_continuation",
                    "supervisor_agent": SUPERVISOR_AGENT_NAME,
                    "executed_by": SUPERVISOR_AGENT_NAME,
                    "model_output": state.get("decision_model_output", {}),
                    "tool_calls": tool_calls,
                    "supervisor_tool_calls": tool_calls,
                    "tool_results": [],
                    "tools_executed": False,
                    "message_flow": ["HumanMessage", "AIMessage(tool_calls)"],
                    "async_continuation_required": True,
                    "async_continuation_type": continuation_type,
                    "policy_confirmation_required": has_deferred_policy_tool_call(tool_calls),
                    "agent_graph_mode": AGENT_GRAPH_MODE,
                    "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
                },
                human_summary=async_continuation_summary(continuation_type),
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _direct_tool_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        executed_calls = state.get("executed_calls", [])
        results = state.get("tool_results", [])
        tool_messages = state.get("tool_messages", [])
        output = state.get("decision_model_output", {})
        final_ai_message = state["final_ai_message"]
        final_model_output = state.get("final_model_output", {})
        structured_payload = {
            "routing_mode": "direct_tool",
            "supervisor_agent": SUPERVISOR_AGENT_NAME,
            "executed_by": SUPERVISOR_AGENT_NAME,
            "supervisor_tool_calls": executed_calls,
            "model_output": output,
            **tool_calls_payload(executed_calls, results),
            "tool_messages": [
                {"name": message.name, "tool_call_id": message.tool_call_id, "content": message.content}
                for message in tool_messages
            ],
            "final_model_output": final_model_output,
            "message_flow": ["HumanMessage", "AIMessage(tool_calls)", "ToolMessage", "AIMessage(final_answer)"],
            "policy_confirmation_required": has_deferred_policy_tool_call(executed_calls),
            "agent_graph_mode": AGENT_GRAPH_MODE,
            "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
            "finalization_mode": LLM_AFTER_TOOL_FINALIZATION_MODE,
        }
        fallback_summary = tool_result_summary(results, natural_chat_summary(output) or "도구를 실행했습니다.")
        final_summary = finalized_chat_summary(final_model_output)
        if final_summary:
            final_answer_source = str(final_model_output.get("fallback") or "model_output")
        else:
            final_summary, final_answer_source = patient_summary_with_source(
                final_ai_message,
                fallback_summary,
                fallback_source="tool_result_summary",
            )
        structured_payload["final_answer_source"] = final_answer_source
        structured_payload["tool_result_summary_used"] = final_answer_source == "tool_result_summary"
        if final_answer_source == "tool_result_summary":
            trace_logging.log_info(
                "agent_final_answer_fallback_used",
                trace_id=state["trace_id"],
                agent_name=SUPERVISOR_AGENT_NAME,
                routing_mode=structured_payload["routing_mode"],
                fallback_source=final_answer_source,
                tool_names=[str(call.get("name") or "") for call in executed_calls],
            )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=SUPERVISOR_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="tool_call",
                structured_payload=structured_payload,
                human_summary=final_summary,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _delegated_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        delegated_response = state["delegated_response"]
        delegated_call = state["delegated_call"]
        final_ai_message = state["final_ai_message"]
        final_model_output = state.get("final_model_output", {})
        final_summary = finalized_chat_summary(final_model_output)
        if final_summary:
            final_answer_source = str(final_model_output.get("fallback") or "model_output")
        else:
            final_summary, final_answer_source = patient_summary_with_source(
                final_ai_message,
                delegated_response.human_summary,
                fallback_source="delegated_agent_summary",
            )
        structured = dict(delegated_response.structured_payload)
        specialist_tool_calls = structured.get("tool_calls") if isinstance(structured.get("tool_calls"), list) else []
        response_decision_type = delegated_response.decision_type
        if response_decision_type != "async_continuation_requested" and any(
            str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in specialist_tool_calls
        ):
            response_decision_type = "side_effect_assessment"
        structured["routing_mode"] = "delegated_agent"
        structured["supervisor_agent"] = SUPERVISOR_AGENT_NAME
        structured["specialist_agent"] = delegated_response.agent_name
        structured["executed_by"] = SUPERVISOR_AGENT_NAME
        structured["delegated_agent"] = delegated_response.agent_name
        structured["delegation_reason"] = delegation_reason(delegated_call)
        structured["delegated_by"] = SUPERVISOR_AGENT_NAME
        structured["supervisor_tool_calls"] = [delegated_call]
        structured["specialist_tool_calls"] = specialist_tool_calls
        structured["delegation_tool_result"] = state["delegated_tool_result"].model_dump(mode="json")
        structured["supervisor_model_output"] = state.get("decision_model_output", {})
        structured["supervisor_final_model_output"] = final_model_output
        structured["final_answer_source"] = final_answer_source
        structured["agent_graph_mode"] = AGENT_GRAPH_MODE
        structured["tool_execution_mode"] = SINGLE_ROUND_TOOL_EXECUTION_MODE
        structured["finalization_mode"] = LLM_AFTER_TOOL_FINALIZATION_MODE
        structured["message_flow"] = [
            "HumanMessage",
            "AIMessage(tool_calls:delegation)",
            "ToolMessage(delegated_agent_result)",
            "AIMessage(final_answer)",
        ]
        return {
            "response": AgentResponse(
                trace_id=delegated_response.trace_id,
                agent_name=SUPERVISOR_AGENT_NAME,
                prompt_version_id=delegated_response.prompt_version_id,
                decision_type=response_decision_type,
                structured_payload=structured,
                human_summary=final_summary,
                requires_conversation_alert=delegated_response.requires_conversation_alert,
                validation_passed=delegated_response.validation_passed,
                validation_errors=delegated_response.validation_errors,
            )
        }

    @staticmethod
    def _validate_output(trace_id: str, output: dict[str, Any], payload: dict[str, Any]) -> None:
        try:
            validate_llm_output("system_guidance", output, payload)
        except Exception as exc:
            trace_logging.log_info(
                "agent_llm_output_validation_failed",
                trace_id=trace_id,
                agent_name=SUPERVISOR_AGENT_NAME,
                decision_type="system_guidance",
                error=safe_exception_summary(exc, limit=300),
                output_keys=sorted(str(key) for key in output.keys()),
            )
            raise AgentExecutionError(
                str(exc),
                error_type="llm_output_validation_failed",
                trace_id=trace_id,
                agent_name=SUPERVISOR_AGENT_NAME,
                decision_type="system_guidance",
            ) from exc


def _model_output_with_tool_calls(
    model_output: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(model_output)
    if tool_calls:
        merged.pop("tool_call", None)
        merged["tool_calls"] = tool_calls
    return merged
