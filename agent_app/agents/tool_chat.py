from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from agent_app import trace_logging
from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    patient_summary_with_source,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.orchestration.confirmations import ConfirmationActionRegistry
from agent_app.orchestration.continuation import async_continuation_summary, async_continuation_type
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID
from agent_app.llm.validation import validate_llm_output
from agent_app.providers.base import BaseLLMProvider
from agent_app.llm.responses import natural_chat_summary, string_list
from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.names import UPDATE_MEDICATION_DOSE_EVENT_STATUS
from agent_app.tools.policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tools.results import tool_calls_payload, tool_result_summary
from agent_app.tools.runtime import ToolRuntime
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse

AGENT_TOOL_LOOP_LIMIT = 6
TOOL_LOOP_MODE = "langgraph_state_graph"
ITERATIVE_TOOL_EXECUTION_MODE = "iterative"
ITERATIVE_FINALIZATION_MODE = "llm_without_tool_calls"


class ToolChatGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    forced_tool_calls: list[dict[str, Any]] | None
    bound_model: Any
    messages: list[Any]
    current_ai_message: Any
    current_model_output: dict[str, Any]
    initial_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    all_executed_calls: list[dict[str, Any]]
    all_results: list[Any]
    all_tool_messages: list[Any]
    iterations: int
    continuation_type: str
    confirmation_required: bool
    forced_tool_calls_seeded: bool
    response: AgentResponse


ResponseBuilder = Callable[[ToolChatGraphState], AgentResponse]


class ToolChatAgentGraph:
    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        tool_runtime: ToolRuntime,
        agent_name: str,
        prompt: str,
        response_mode: str,
        decision_type: str,
        tool_names: tuple[str, ...],
        source_event_type: str,
        force_ae_after_positive_lookup: bool = False,
        defer_async_continuation: bool = True,
        validation_decision_type: str | None = None,
        response_builder: ResponseBuilder | None = None,
        requires_conversation_alert: bool = False,
        max_iterations: int = AGENT_TOOL_LOOP_LIMIT,
    ) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.agent_name = agent_name
        self.prompt = prompt
        self.response_mode = response_mode
        self.decision_type = decision_type
        self.tool_names = tool_names
        self.source_event_type = source_event_type
        self.force_ae_after_positive_lookup = force_ae_after_positive_lookup
        self.defer_async_continuation = defer_async_continuation
        self.validation_decision_type = validation_decision_type
        self.response_builder = response_builder
        self.requires_conversation_alert = requires_conversation_alert
        self.max_iterations = max_iterations
        self.graph = self._build_graph()

    async def invoke(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        forced_tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        payload = dict(request_payload)
        catalog_tools = ToolCatalog.tools_for(*self.tool_names)
        initial_messages = build_chat_messages(
            self.prompt,
            {
                **payload,
                "response_mode": self.response_mode,
                "decision_type": self.decision_type,
                "available_tools": catalog_tools,
            },
        )
        final_state = await self.graph.ainvoke(
            {
                "trace_id": trace_id,
                "request_payload": payload,
                "forced_tool_calls": list(forced_tool_calls) if forced_tool_calls is not None else None,
                "messages": initial_messages,
                "all_executed_calls": [],
                "all_results": [],
                "all_tool_messages": [],
                "iterations": 0,
                "forced_tool_calls_seeded": False,
            }
        )
        return final_state["response"]

    def _build_graph(self):
        graph = StateGraph(ToolChatGraphState)
        graph.add_node("prepare_model", self._prepare_model)
        graph.add_node("llm_call", self._llm_call)
        graph.add_node("tool_node", self._tool_node)
        graph.add_node("final_response", self._final_response)
        graph.add_node("async_continuation_response", self._async_continuation_response)
        graph.add_node("confirmation_response", self._confirmation_response)
        graph.add_node("max_iterations_response", self._max_iterations_response)
        graph.set_entry_point("prepare_model")
        graph.add_edge("prepare_model", "llm_call")
        graph.add_conditional_edges(
            "llm_call",
            self._route_after_llm,
            {
                "tool_node": "tool_node",
                "final_response": "final_response",
                "async_continuation_response": "async_continuation_response",
                "max_iterations_response": "max_iterations_response",
            },
        )
        graph.add_conditional_edges(
            "tool_node",
            self._route_after_tool,
            {
                "llm_call": "llm_call",
                "confirmation_response": "confirmation_response",
            },
        )
        graph.add_edge("final_response", END)
        graph.add_edge("async_continuation_response", END)
        graph.add_edge("confirmation_response", END)
        graph.add_edge("max_iterations_response", END)
        return graph.compile()

    def _prepare_model(self, state: ToolChatGraphState) -> dict[str, Any]:
        catalog_tools = ToolCatalog.tools_for(*self.tool_names)
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        return {"bound_model": bound_model}

    async def _llm_call(self, state: ToolChatGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        forced_tool_calls = state.get("forced_tool_calls")
        if forced_tool_calls is not None and not state.get("forced_tool_calls_seeded", False):
            output = {"message": request_payload.get("message"), "observations": ["forced_tool_calls"]}
            tool_calls = normalize_policy_tool_calls(forced_tool_calls, source_event_type=self.source_event_type)
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(request_payload.get("message") or ""),
                model_output=output,
            )
            forced_tool_calls_seeded = True
        else:
            ai_message = await state["bound_model"].ainvoke(state["messages"])
            output = model_output_from_ai_message(ai_message)
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type=self.source_event_type,
            )
            forced_tool_calls_seeded = state.get("forced_tool_calls_seeded", False)

        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(ai_message.content or ""),
                model_output=output,
            )

        validation_output = _model_output_with_tool_calls(output, tool_calls)
        self._validate_output(
            state["trace_id"],
            validation_output,
            request_payload,
            allow_tool_only=bool(tool_calls),
        )
        output = validation_output

        updates: dict[str, Any] = {
            "current_ai_message": ai_message,
            "current_model_output": output,
            "pending_tool_calls": tool_calls,
            "continuation_type": async_continuation_type(tool_calls),
            "forced_tool_calls_seeded": forced_tool_calls_seeded,
        }
        if "initial_model_output" not in state:
            updates["initial_model_output"] = dict(output)
        return updates

    def _route_after_llm(self, state: ToolChatGraphState) -> str:
        tool_calls = state.get("pending_tool_calls", [])
        continuation_type = state.get("continuation_type", "")
        if (
            self.defer_async_continuation
            and continuation_type
            and state.get("forced_tool_calls") is None
            and not any(str(call.get("name") or "") == UPDATE_MEDICATION_DOSE_EVENT_STATUS for call in tool_calls)
        ):
            return "async_continuation_response"
        if not tool_calls:
            return "final_response"
        if state.get("iterations", 0) >= self.max_iterations:
            return "max_iterations_response"
        return "tool_node"

    async def _tool_node(self, state: ToolChatGraphState) -> dict[str, Any]:
        tool_calls = state.get("pending_tool_calls", [])
        output = state.get("current_model_output", {})
        ai_message = state["current_ai_message"]
        executed_calls, results = await self.tool_runtime.execute(
            tool_calls,
            trace_id=state["trace_id"],
            source_event_type=self.source_event_type,
            payload=state["request_payload"],
            force_ae_after_positive_lookup=self.force_ae_after_positive_lookup,
            routing_context={
                "routing_mode": "specialist_tool",
                "executed_by": self.agent_name,
                "specialist_agent": self.agent_name,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "agent_graph_mode": TOOL_LOOP_MODE,
                "specialist_tool_names": [str(call.get("name") or "") for call in tool_calls],
                "tool_names": [str(call.get("name") or "") for call in tool_calls],
            },
        )
        executed_ai_message = ai_message_from_tool_calls(executed_calls, content=str(ai_message.content or ""), model_output=output) if executed_calls else ai_message
        tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
        confirmation_required = any(result.status == "confirmation_required" for result in results)
        if not confirmation_required:
            failed_mutation = next(
                (
                    result
                    for result in results
                    if ConfirmationActionRegistry.requires_confirmation(result.tool_name)
                    and (
                        result.status == "skipped"
                        or str(result.error or "").startswith("mutation_confirmation_")
                    )
                ),
                None,
            )
            if failed_mutation is not None:
                raise AgentExecutionError(
                    f"mutation_tool_not_applied:{failed_mutation.tool_name}:{failed_mutation.status}",
                    error_type="mutation_tool_not_applied",
                    trace_id=state["trace_id"],
                    agent_name=self.agent_name,
                    decision_type=self.decision_type,
                )
        return {
            "messages": [*state["messages"], executed_ai_message, *tool_messages],
            "all_executed_calls": [*state.get("all_executed_calls", []), *executed_calls],
            "all_results": [*state.get("all_results", []), *results],
            "all_tool_messages": [*state.get("all_tool_messages", []), *tool_messages],
            "iterations": state.get("iterations", 0) + 1,
            "pending_tool_calls": [],
            "confirmation_required": confirmation_required,
        }

    @staticmethod
    def _route_after_tool(state: ToolChatGraphState) -> str:
        return "confirmation_response" if state.get("confirmation_required") else "llm_call"

    def _confirmation_response(self, state: ToolChatGraphState) -> dict[str, AgentResponse]:
        all_executed_calls = state.get("all_executed_calls", [])
        all_results = state.get("all_results", [])
        all_tool_messages = state.get("all_tool_messages", [])
        proposal = _first_mutation_confirmation(all_results)
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="mutation_confirmation_required",
                structured_payload={
                    "routing_mode": "specialist_confirmation_required",
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "executed_by": self.agent_name,
                    "specialist_agent": self.agent_name,
                    "specialist_tool_calls": all_executed_calls,
                    "model_output": state.get("initial_model_output", {}),
                    **tool_calls_payload(all_executed_calls, all_results),
                    "tool_messages": [
                        {
                            "name": message.name,
                            "tool_call_id": message.tool_call_id,
                            "content": message.content,
                        }
                        for message in all_tool_messages
                    ],
                    "mutation_confirmation_required": True,
                    "mutation_confirmation": proposal,
                    "message_flow": tool_chat_message_flow(len(all_results), pending_confirmation=True),
                    "iterations": state.get("iterations", 0),
                },
                human_summary="?? ??? ???? ?? ??? ??? ?????.",
                requires_conversation_alert=False,
            )
        }

    def _async_continuation_response(self, state: ToolChatGraphState) -> dict[str, AgentResponse]:
        continuation_type = state.get("continuation_type", "")
        tool_calls = state.get("pending_tool_calls", [])
        all_executed_calls = state.get("all_executed_calls", [])
        all_results = state.get("all_results", [])
        summary = async_continuation_summary(continuation_type)
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="async_continuation_requested",
                structured_payload={
                    "routing_mode": "specialist_async_continuation",
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "executed_by": self.agent_name,
                    "specialist_agent": self.agent_name,
                    "specialist_tool_calls": [*all_executed_calls, *tool_calls],
                    "model_output": state.get("initial_model_output", {}),
                    "final_model_output": state.get("current_model_output", {}),
                    "tool_calls": [*all_executed_calls, *tool_calls],
                    "tool_results": [result.model_dump(mode="json") for result in all_results],
                    "tools_executed": bool(all_executed_calls),
                    "message_flow": tool_chat_message_flow(len(all_results), pending_tool_call=True),
                    "async_continuation_required": True,
                    "async_continuation_type": continuation_type,
                    "policy_confirmation_required": has_deferred_policy_tool_call(tool_calls),
                },
                human_summary=summary,
                requires_conversation_alert=self.requires_conversation_alert,
            )
        }

    def _max_iterations_response(self, state: ToolChatGraphState) -> dict[str, AgentResponse]:
        all_executed_calls = state.get("all_executed_calls", [])
        all_results = state.get("all_results", [])
        summary = f"전문 에이전트의 도구 실행 단계가 {self.max_iterations}회를 초과해 요청을 완료하지 못했습니다. 요청을 조금 더 구체적으로 나누어 다시 시도해 주세요."
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type=self.decision_type,
                structured_payload={
                    "routing_mode": "specialist_max_iterations",
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "executed_by": self.agent_name,
                    "specialist_agent": self.agent_name,
                    "specialist_tool_calls": all_executed_calls,
                    "pending_tool_calls": state.get("pending_tool_calls", []),
                    "model_output": state.get("initial_model_output", {}),
                    "final_model_output": state.get("current_model_output", {}),
                    **tool_calls_payload(all_executed_calls, all_results),
                    "message_flow": tool_chat_message_flow(len(all_results), pending_tool_call=True),
                    "policy_confirmation_required": has_deferred_policy_tool_call(all_executed_calls),
                },
                human_summary=summary,
                requires_conversation_alert=self.requires_conversation_alert,
            )
        }

    def _final_response(self, state: ToolChatGraphState) -> dict[str, AgentResponse]:
        all_executed_calls = state.get("all_executed_calls", [])
        all_results = state.get("all_results", [])
        all_tool_messages = state.get("all_tool_messages", [])
        if self.response_builder is not None:
            return {"response": self.response_builder(state)}

        if not all_executed_calls:
            return {"response": self._specialist_answer_response(state)}

        final_ai_message = state["current_ai_message"]
        final_model_output = state.get("current_model_output", {})
        initial_model_output = state.get("initial_model_output", {})
        structured_payload = {
            "routing_mode": "specialist_tool",
            "tool_loop_mode": TOOL_LOOP_MODE,
            "agent_graph_mode": TOOL_LOOP_MODE,
            "executed_by": self.agent_name,
            "specialist_agent": self.agent_name,
            "specialist_tool_calls": all_executed_calls,
            "model_output": initial_model_output,
            **tool_calls_payload(all_executed_calls, all_results),
            "tool_messages": [
                {
                    "name": message.name,
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
                for message in all_tool_messages
            ],
            "final_model_output": final_model_output,
            "message_flow": tool_chat_message_flow(len(all_results)),
            "policy_confirmation_required": has_deferred_policy_tool_call(all_executed_calls),
        }
        fallback_summary = tool_result_summary(all_results, natural_chat_summary(initial_model_output) or "도구를 실행했습니다.")
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
                agent_name=self.agent_name,
                routing_mode=structured_payload["routing_mode"],
                tool_loop_mode=structured_payload["tool_loop_mode"],
                fallback_source=final_answer_source,
                tool_names=[str(call.get("name") or "") for call in all_executed_calls],
            )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type=self.decision_type,
                structured_payload=structured_payload,
                human_summary=final_summary,
                requires_conversation_alert=self.requires_conversation_alert,
            )
        }

    def _specialist_answer_response(self, state: ToolChatGraphState) -> AgentResponse:
        ai_message = state["current_ai_message"]
        output = state.get("current_model_output", {})
        human_summary, final_answer_source = patient_summary_with_source(
            ai_message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        return AgentResponse(
            trace_id=state["trace_id"],
            agent_name=self.agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type=self.decision_type,
            structured_payload={
                "routing_mode": "specialist_answer",
                "tool_loop_mode": TOOL_LOOP_MODE,
                "agent_graph_mode": TOOL_LOOP_MODE,
                "executed_by": self.agent_name,
                "specialist_agent": self.agent_name,
                "specialist_tool_calls": [],
                "observations": string_list(output.get("observations")),
                "model_output": output,
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "final_answer_source": final_answer_source,
                "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
            },
            human_summary=human_summary,
            requires_conversation_alert=self.requires_conversation_alert,
        )

    def _validate_output(
        self,
        trace_id: str,
        output: dict[str, Any],
        payload: dict[str, Any],
        *,
        allow_tool_only: bool,
    ) -> None:
        if self.validation_decision_type is None:
            return
        try:
            validate_llm_output(
                self.validation_decision_type,
                output,
                payload,
                allow_tool_only=allow_tool_only,
            )
        except Exception as exc:
            trace_logging.log_info(
                "agent_llm_output_validation_failed",
                trace_id=trace_id,
                agent_name=self.agent_name,
                decision_type=self.validation_decision_type,
                error=safe_exception_summary(exc, limit=300),
                output_keys=sorted(str(key) for key in output.keys()),
            )
            raise AgentExecutionError(
                str(exc),
                error_type="llm_output_validation_failed",
                trace_id=trace_id,
                agent_name=self.agent_name,
                decision_type=self.validation_decision_type,
            ) from exc


def tool_chat_message_flow(
    tool_result_count: int,
    *,
    pending_tool_call: bool = False,
    pending_confirmation: bool = False,
) -> list[str]:
    flow = ["HumanMessage"]
    for _ in range(tool_result_count):
        flow.extend(["AIMessage(tool_calls)", "ToolMessage"])
    if pending_tool_call or pending_confirmation:
        flow.append("AIMessage(tool_calls)")
    else:
        flow.append("AIMessage(final_answer)")
    return flow


def _first_mutation_confirmation(results: list[Any]) -> dict[str, Any]:
    for result in results:
        if getattr(result, "status", "") != "confirmation_required":
            continue
        response = result.response if isinstance(getattr(result, "response", None), dict) else {}
        proposal = response.get("mutation_confirmation")
        if isinstance(proposal, dict):
            return proposal
    return {}


def _model_output_with_tool_calls(
    model_output: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(model_output)
    if tool_calls:
        merged.pop("tool_call", None)
        merged["tool_calls"] = tool_calls
    return merged
