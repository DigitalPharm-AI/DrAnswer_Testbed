from __future__ import annotations

from time import perf_counter
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
from agent_app.agents.tool_chat import (
    AGENT_TOOL_LOOP_LIMIT,
    ITERATIVE_FINALIZATION_MODE,
    ITERATIVE_TOOL_EXECUTION_MODE,
    TOOL_LOOP_MODE,
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
from agent_app.output_validation import validate_llm_output, validate_mutation_confirmation_reply_output
from agent_app.prompt_builders import (
    multiturn_chat_prompt,
    mutation_confirmation_prompt,
    mutation_confirmation_reply_prompt,
    mutation_resolution_prompt,
)
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
MULTITURN_CHAT_AGENT_NAME = "multiturn_chat_agent"


class MultiturnGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    context: dict[str, Any]
    messages: list[Any]
    bound_model: Any
    current_ai_message: Any
    current_model_output: dict[str, Any]
    initial_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    continuation_type: str
    forced_specialist_tool_calls: list[dict[str, Any]]
    forced_tool_calls_seeded: bool
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
                    "forced_specialist_tool_calls": [],
                    "forced_tool_calls_seeded": False,
                    "all_supervisor_tool_calls": [],
                    "all_supervisor_tool_results": [],
                    "all_tool_messages": [],
                    "delegation_calls": [],
                    "delegated_responses": [],
                    "direct_tool_calls": [],
                    "direct_tool_results": [],
                    "tool_round_result_counts": [],
                    "iterations": 0,
                }
            )
            response = final_state["response"]
            reply_intent = str(
                final_state.get("confirmation_reply_intent")
                or context.get("pending_mutation_confirmation_reply_resolved")
                or ""
            )
            if reply_intent:
                structured_payload = dict(response.structured_payload)
                structured_payload["mutation_confirmation_reply"] = {"intent": reply_intent}
                response = response.model_copy(update={"structured_payload": structured_payload})
            return response
        except Exception as exc:
            raise agent_error(trace_id, MULTITURN_CHAT_AGENT_NAME, "system_guidance", exc) from exc

    def _build_graph(self):
        graph = StateGraph(MultiturnGraphState)
        graph.add_node("entry_router", self._entry_router)
        graph.add_node("prepare_model", self._prepare_model)
        graph.add_node("supervisor_llm", self._supervisor_llm)
        graph.add_node("supervisor_tool_node", self._supervisor_tool_node)
        graph.add_node("confirmation_llm", self._confirmation_llm)
        graph.add_node("confirmation_response", self._confirmation_response)
        graph.add_node("prepare_confirmation_reply_model", self._prepare_confirmation_reply_model)
        graph.add_node("confirmation_reply_llm", self._confirmation_reply_llm)
        graph.add_node("confirmation_reply_response", self._confirmation_reply_response)
        graph.add_node("prepare_mutation_resolution_model", self._prepare_mutation_resolution_model)
        graph.add_node("mutation_resolution_response", self._mutation_resolution_response)
        graph.add_node("final_response", self._final_response)
        graph.add_node("async_continuation_response", self._async_continuation_response)
        graph.add_node("max_iterations_response", self._max_iterations_response)
        graph.add_edge(START, "entry_router")
        graph.add_conditional_edges(
            "entry_router",
            self._route_after_entry,
            {
                "prepare_model": "prepare_model",
                "prepare_mutation_resolution_model": "prepare_mutation_resolution_model",
                "prepare_confirmation_reply_model": "prepare_confirmation_reply_model",
            },
        )
        graph.add_edge("prepare_confirmation_reply_model", "confirmation_reply_llm")
        graph.add_conditional_edges(
            "confirmation_reply_llm",
            self._route_after_confirmation_reply,
            {
                "prepare_model": "prepare_model",
                "confirmation_reply_response": "confirmation_reply_response",
            },
        )
        graph.add_edge("prepare_model", "supervisor_llm")
        graph.add_conditional_edges(
            "supervisor_llm",
            self._route_after_llm,
            {
                "supervisor_tool_node": "supervisor_tool_node",
                "final_response": "final_response",
                "mutation_resolution_response": "mutation_resolution_response",
                "async_continuation_response": "async_continuation_response",
                "max_iterations_response": "max_iterations_response",
            },
        )
        graph.add_conditional_edges(
            "supervisor_tool_node",
            self._route_after_tool,
            {
                "supervisor_llm": "supervisor_llm",
                "confirmation_llm": "confirmation_llm",
            },
        )
        graph.add_edge("confirmation_llm", "confirmation_response")
        graph.add_edge("prepare_mutation_resolution_model", "supervisor_llm")
        graph.add_edge("final_response", END)
        graph.add_edge("async_continuation_response", END)
        graph.add_edge("max_iterations_response", END)
        graph.add_edge("confirmation_response", END)
        graph.add_edge("mutation_resolution_response", END)
        graph.add_edge("confirmation_reply_response", END)
        return graph.compile()

    @staticmethod
    def _entry_router(state: MultiturnGraphState) -> dict[str, str]:
        context = state.get("context", {})
        resolution = context.get("mutation_resolution")
        pending_confirmation = context.get("pending_mutation_confirmation")
        if isinstance(resolution, dict):
            entry_mode = "mutation_resolution"
        elif isinstance(pending_confirmation, dict) and pending_confirmation:
            entry_mode = "mutation_confirmation_reply"
        else:
            entry_mode = "standard"
        return {"entry_mode": entry_mode}

    @staticmethod
    def _route_after_entry(state: MultiturnGraphState) -> str:
        if state.get("entry_mode") == "mutation_resolution":
            return "prepare_mutation_resolution_model"
        if state.get("entry_mode") == "mutation_confirmation_reply":
            return "prepare_confirmation_reply_model"
        return "prepare_model"

    @staticmethod
    def _prepare_confirmation_reply_model(state: MultiturnGraphState) -> dict[str, Any]:
        pending_confirmation = state.get("context", {}).get("pending_mutation_confirmation")
        if not isinstance(pending_confirmation, dict):
            raise ValueError("pending_mutation_confirmation_required")
        messages = build_chat_messages(
            mutation_confirmation_reply_prompt(),
            {
                "response_mode": "mutation_confirmation_reply",
                "user_reply": state["request_payload"].get("message"),
                "pending_change": {
                    "display": pending_confirmation.get("display", {}),
                    "original_request": pending_confirmation.get("original_request", ""),
                },
            },
        )
        return {"messages": messages}

    async def _confirmation_reply_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        started = perf_counter()
        ai_message = await self.provider.chat_model().ainvoke(state["messages"])
        elapsed_ms = round((perf_counter() - started) * 1000)
        native_tool_calls = tool_calls_from_ai_message(ai_message)
        output = model_output_from_ai_message(ai_message)
        serialized_tool_calls = output.get("tool_calls")
        if native_tool_calls or isinstance(output.get("tool_call"), dict) or (
            isinstance(serialized_tool_calls, list) and serialized_tool_calls
        ):
            raise AgentExecutionError(
                "mutation_confirmation_reply_returned_tool_calls",
                error_type="llm_output_validation_failed",
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="mutation_confirmation_reply",
            )
        validated = validate_mutation_confirmation_reply_output(output)
        trace_logging.log_info(
            "agent_llm_node_completed",
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            node="mutation_confirmation_reply_llm",
            elapsed_ms=elapsed_ms,
            tools_bound=False,
            intent=validated["intent"],
        )
        updates: dict[str, Any] = {
            "confirmation_reply_ai_message": ai_message,
            "confirmation_reply_model_output": validated,
            "confirmation_reply_intent": validated["intent"],
            "confirmation_reply_llm_elapsed_ms": elapsed_ms,
        }
        if validated["intent"] == "new_request":
            context = dict(state.get("context", {}))
            context.pop("pending_mutation_confirmation", None)
            request_metadata = dict(context.get("request_metadata") or {})
            request_metadata.pop("pending_mutation_confirmation", None)
            request_metadata.pop("pending_mutation_confirmation_id", None)
            context["request_metadata"] = request_metadata
            context["pending_mutation_confirmation_reply_resolved"] = "new_request"
            request_payload = dict(state["request_payload"])
            request_payload["context"] = context
            updates["context"] = context
            updates["request_payload"] = request_payload
        return updates

    @staticmethod
    def _route_after_confirmation_reply(state: MultiturnGraphState) -> str:
        if state.get("confirmation_reply_intent") == "new_request":
            return "prepare_model"
        return "confirmation_reply_response"

    @staticmethod
    def _confirmation_reply_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        output = state["confirmation_reply_model_output"]
        human_summary, final_answer_source = patient_summary_with_source(
            state["confirmation_reply_ai_message"],
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        intent = str(output["intent"])
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="mutation_confirmation_reply",
                structured_payload={
                    "routing_mode": "mutation_confirmation_reply",
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "mutation_confirmation_reply": {"intent": intent},
                    "model_output": output,
                    "tool_calls": [],
                    "tool_results": [],
                    "tools_executed": False,
                    "final_answer_source": final_answer_source,
                    "message_flow": ["HumanMessage", "AIMessage(confirmation_reply)"],
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": "none",
                    "finalization_mode": "confirmation_reply_interpretation",
                    "node_timings_ms": {
                        "mutation_confirmation_reply_llm": state.get("confirmation_reply_llm_elapsed_ms", 0),
                    },
                    "iterations": 0,
                },
                human_summary=human_summary,
                requires_conversation_alert=False,
            )
        }

    def _prepare_mutation_resolution_model(self, state: MultiturnGraphState) -> dict[str, Any]:
        resolution = state.get("context", {}).get("mutation_resolution")
        request_payload = state["request_payload"]
        catalog_tools = [*ToolCatalog.tools_for(*SUPERVISOR_DIRECT_TOOLS), *delegation_tools_payload()]
        messages = build_chat_messages(
            mutation_resolution_prompt(),
            {
                **request_payload,
                "response_mode": "mutation_resolution_continuation",
                "original_request": request_payload.get("message"),
                "context": {**state.get("context", {}), "mutation_resolution": resolution},
                "available_tools": catalog_tools,
            },
        )
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        return {"messages": messages, "bound_model": bound_model}

    @staticmethod
    def _mutation_resolution_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        ai_message = state["current_ai_message"]
        output = state.get("current_model_output", {})
        human_summary, final_answer_source = patient_summary_with_source(
            ai_message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        resolution = state.get("context", {}).get("mutation_resolution")
        supervisor_calls = state.get("all_supervisor_tool_calls", [])
        supervisor_results = state.get("all_supervisor_tool_results", [])
        delegated_responses = state.get("delegated_responses", [])
        specialist_calls, specialist_results, specialist_messages = _specialist_tool_payloads(delegated_responses)
        direct_calls = state.get("direct_tool_calls", [])
        direct_results = state.get("direct_tool_results", [])
        tool_calls = [*specialist_calls, *direct_calls]
        tool_results = [*specialist_results, *(result.model_dump(mode="json") for result in direct_results)]
        timings = state.get("mutation_resolution_llm_timings_ms", [])
        routing_mode = "mutation_resolution_continuation" if supervisor_calls else "mutation_resolution_finalization"
        delegated_agents = [response.agent_name for response in delegated_responses]
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="mutation_resolution",
                structured_payload={
                    "routing_mode": routing_mode,
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "mutation_resolution": resolution if isinstance(resolution, dict) else {},
                    "model_output": output,
                    "supervisor_tool_calls": supervisor_calls,
                    "supervisor_tool_results": [result.model_dump(mode="json") for result in supervisor_results],
                    "delegated_agents": delegated_agents,
                    "specialist_tool_calls": specialist_calls,
                    "tool_calls": tool_calls,
                    "tool_results": tool_results,
                    "tool_messages": specialist_messages,
                    "tools_executed": bool(tool_calls),
                    "final_answer_source": final_answer_source,
                    "message_flow": _supervisor_message_flow(state.get("tool_round_result_counts", [])),
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "finalization_mode": "mutation_resolution_iterative_llm",
                    "node_timings_ms": {
                        "mutation_resolution_llm": sum(timings),
                    },
                    "node_timing_samples_ms": {"mutation_resolution_llm": timings},
                    "iterations": state.get("iterations", 0),
                },
                human_summary=human_summary,
                requires_conversation_alert=any(response.requires_conversation_alert for response in delegated_responses),
                validation_passed=all(response.validation_passed for response in delegated_responses),
                validation_errors=[error for response in delegated_responses for error in (response.validation_errors or [])],
            )
        }

    def _prepare_model(self, state: MultiturnGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
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
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        return {"messages": messages, "bound_model": bound_model}

    async def _supervisor_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        context = state.get("context", {})
        async_tool_calls = context.get("async_tool_calls")
        forced_tool_calls_seeded = state.get("forced_tool_calls_seeded", False)
        forced_specialist_tool_calls = state.get("forced_specialist_tool_calls", [])
        resolution_elapsed_ms: int | None = None

        if context.get("execute_async_continuation") is True and isinstance(async_tool_calls, list) and not forced_tool_calls_seeded:
            output = {"message": request_payload.get("message"), "observations": ["async_continuation"]}
            tool_calls = normalize_policy_tool_calls(async_tool_calls, source_event_type="multiturn_chat")
            if any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in tool_calls):
                forced_specialist_tool_calls = tool_calls
                tool_calls = [
                    {
                        "name": DELEGATE_TO_MEDICATION_AGENT,
                        "arguments": {
                            "task": "Continue the deferred medication side-effect assessment.",
                            "reason": "async_medication_continuation",
                        },
                    }
                ]
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(request_payload.get("message") or ""),
                model_output=output,
            )
            forced_tool_calls_seeded = True
        else:
            started = perf_counter()
            ai_message = await state["bound_model"].ainvoke(state["messages"])
            if state.get("entry_mode") == "mutation_resolution":
                resolution_elapsed_ms = round((perf_counter() - started) * 1000)
                trace_logging.log_info(
                    "agent_llm_node_completed",
                    trace_id=state["trace_id"],
                    agent_name=MULTITURN_CHAT_AGENT_NAME,
                    node="mutation_resolution_llm",
                    elapsed_ms=resolution_elapsed_ms,
                    tools_bound=True,
                )
            output = model_output_from_ai_message(ai_message)
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type="multiturn_chat",
            )

        validation_output = _model_output_with_tool_calls(output, tool_calls)
        self._validate_output(state["trace_id"], validation_output, request_payload)
        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(ai_message.content or ""),
                model_output=validation_output,
            )

        updates: dict[str, Any] = {
            "current_ai_message": ai_message,
            "current_model_output": validation_output,
            "pending_tool_calls": tool_calls,
            "continuation_type": async_continuation_type(tool_calls),
            "forced_specialist_tool_calls": forced_specialist_tool_calls,
            "forced_tool_calls_seeded": forced_tool_calls_seeded,
        }
        if "initial_model_output" not in state:
            updates["initial_model_output"] = dict(validation_output)
        if resolution_elapsed_ms is not None:
            updates["mutation_resolution_llm_timings_ms"] = [
                *state.get("mutation_resolution_llm_timings_ms", []),
                resolution_elapsed_ms,
            ]
        return updates

    @staticmethod
    def _route_after_llm(state: MultiturnGraphState) -> str:
        tool_calls = state.get("pending_tool_calls", [])
        continuation_type = state.get("continuation_type", "")
        if continuation_type and state.get("context", {}).get("execute_async_continuation") is not True:
            return "async_continuation_response"
        if not tool_calls:
            if state.get("entry_mode") == "mutation_resolution":
                return "mutation_resolution_response"
            return "final_response"
        if state.get("iterations", 0) >= AGENT_TOOL_LOOP_LIMIT:
            return "max_iterations_response"
        return "supervisor_tool_node"

    async def _supervisor_tool_node(self, state: MultiturnGraphState) -> dict[str, Any]:
        pending_calls = state.get("pending_tool_calls", [])
        executed_calls: list[dict[str, Any]] = []
        results: list[ToolCallResult] = []
        delegation_calls: list[dict[str, Any]] = []
        delegated_responses: list[AgentResponse] = []
        direct_calls: list[dict[str, Any]] = []
        direct_results: list[ToolCallResult] = []
        forced_specialist_tool_calls = state.get("forced_specialist_tool_calls", [])
        prior_delegation_count = len(state.get("delegation_calls", []))

        for call_index, call in enumerate(pending_calls):
            if is_delegation_tool_call(call):
                delegation_payload = self._delegation_request_payload(state, call)
                delegated_response = await self._run_delegation(
                    state["trace_id"],
                    delegation_payload,
                    call,
                    forced_tool_calls=forced_specialist_tool_calls or None,
                )
                forced_specialist_tool_calls = []
                result = self._delegation_tool_result(
                    state["trace_id"],
                    call,
                    delegated_response,
                    sequence=prior_delegation_count + len(delegation_calls),
                )
                executed_calls.append(call)
                results.append(result)
                delegation_calls.append(call)
                delegated_responses.append(delegated_response)
                if result.status == "confirmation_required":
                    self._append_blocked_supervisor_calls(
                        pending_calls[call_index + 1 :],
                        executed_calls,
                        results,
                        trace_id=state["trace_id"],
                        confirmation_tool=result.tool_name,
                    )
                    break
                continue

            runtime_calls, runtime_results = await self.tool_runtime.execute(
                [call],
                trace_id=state["trace_id"],
                source_event_type="multiturn_chat",
                payload=state["request_payload"],
                routing_context={
                    "routing_mode": "supervisor_tool_loop",
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "supervisor_tool_names": [str(call.get("name") or "")],
                    "tool_names": [str(call.get("name") or "")],
                },
            )
            if len(runtime_calls) != len(runtime_results):
                raise RuntimeError("supervisor_tool_execution_result_count_mismatch")
            executed_calls.extend(runtime_calls)
            results.extend(runtime_results)
            direct_calls.extend(runtime_calls)
            direct_results.extend(runtime_results)
            if any(result.status == "confirmation_required" for result in runtime_results):
                self._append_blocked_supervisor_calls(
                    pending_calls[call_index + 1 :],
                    executed_calls,
                    results,
                    trace_id=state["trace_id"],
                    confirmation_tool=runtime_results[-1].tool_name,
                )
                break

        if len(executed_calls) != len(results):
            raise RuntimeError("supervisor_tool_execution_result_count_mismatch")

        ai_message = state["current_ai_message"]
        output = state.get("current_model_output", {})
        executed_ai_message = ai_message_from_tool_calls(
            executed_calls,
            content=str(ai_message.content or ""),
            model_output=output,
        )
        tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
        return {
            "messages": [*state["messages"], executed_ai_message, *tool_messages],
            "all_supervisor_tool_calls": [*state.get("all_supervisor_tool_calls", []), *executed_calls],
            "all_supervisor_tool_results": [*state.get("all_supervisor_tool_results", []), *results],
            "all_tool_messages": [*state.get("all_tool_messages", []), *tool_messages],
            "delegation_calls": [*state.get("delegation_calls", []), *delegation_calls],
            "delegated_responses": [*state.get("delegated_responses", []), *delegated_responses],
            "direct_tool_calls": [*state.get("direct_tool_calls", []), *direct_calls],
            "direct_tool_results": [*state.get("direct_tool_results", []), *direct_results],
            "tool_round_result_counts": [*state.get("tool_round_result_counts", []), len(results)],
            "iterations": state.get("iterations", 0) + 1,
            "pending_tool_calls": [],
            "forced_specialist_tool_calls": forced_specialist_tool_calls,
            "confirmation_required": any(result.status == "confirmation_required" for result in results),
        }

    @staticmethod
    def _route_after_tool(state: MultiturnGraphState) -> str:
        return "confirmation_llm" if state.get("confirmation_required") else "supervisor_llm"

    @staticmethod
    def _append_blocked_supervisor_calls(
        blocked_calls: list[dict[str, Any]],
        executed_calls: list[dict[str, Any]],
        results: list[ToolCallResult],
        *,
        trace_id: str,
        confirmation_tool: str,
    ) -> None:
        for index, blocked_call in enumerate(blocked_calls):
            executed_calls.append(blocked_call)
            results.append(
                ToolCallResult(
                    tool_name=str(blocked_call.get("name") or "unknown"),
                    status="skipped",
                    response={
                        "reason": "blocked_by_pending_confirmation",
                        "confirmation_tool": confirmation_tool,
                    },
                    error="blocked_by_pending_confirmation",
                    idempotency_key=f"{trace_id}:{blocked_call.get('name') or 'unknown'}:supervisor-blocked:{index}",
                )
            )

    async def _confirmation_llm(self, state: MultiturnGraphState) -> dict[str, Any]:
        proposal = _mutation_confirmation_from_state(state)
        messages = build_chat_messages(
            mutation_confirmation_prompt(),
            {
                "original_request": state["request_payload"].get("message"),
                "mutation_confirmation": proposal,
            },
        )
        ai_message = await self.provider.chat_model().ainvoke(messages)
        native_tool_calls = tool_calls_from_ai_message(ai_message)
        output = model_output_from_ai_message(ai_message)
        serialized_tool_calls = output.get("tool_calls")
        if native_tool_calls or isinstance(output.get("tool_call"), dict) or (
            isinstance(serialized_tool_calls, list) and serialized_tool_calls
        ):
            raise AgentExecutionError(
                "mutation_confirmation_finalizer_returned_tool_calls",
                error_type="llm_output_validation_failed",
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="mutation_confirmation_required",
            )
        self._validate_output(state["trace_id"], output, state["request_payload"])
        return {
            "confirmation_ai_message": ai_message,
            "confirmation_model_output": output,
        }

    @staticmethod
    def _confirmation_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        proposal = _mutation_confirmation_from_state(state)
        ai_message = state["confirmation_ai_message"]
        output = state.get("confirmation_model_output", {})
        human_summary, final_answer_source = patient_summary_with_source(
            ai_message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        specialist_tool_calls, specialist_tool_results, specialist_tool_messages = _specialist_tool_payloads(
            state.get("delegated_responses", [])
        )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="mutation_confirmation_required",
                structured_payload={
                    "routing_mode": "mutation_confirmation_required",
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "supervisor_tool_calls": state.get("all_supervisor_tool_calls", []),
                    "supervisor_tool_results": [
                        result.model_dump(mode="json")
                        for result in state.get("all_supervisor_tool_results", [])
                    ],
                    "specialist_tool_calls": specialist_tool_calls,
                    "tool_calls": specialist_tool_calls,
                    "tool_results": specialist_tool_results,
                    "tool_messages": specialist_tool_messages,
                    "mutation_confirmation_required": True,
                    "mutation_confirmation": proposal,
                    "model_output": state.get("initial_model_output", {}),
                    "final_model_output": output,
                    "final_answer_source": final_answer_source,
                    "message_flow": _supervisor_message_flow(state.get("tool_round_result_counts", [])),
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "finalization_mode": "confirmation_llm_without_tools",
                    "iterations": state.get("iterations", 0),
                },
                human_summary=human_summary,
                requires_conversation_alert=False,
            )
        }

    async def _run_delegation(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        delegated_call: dict[str, Any],
        *,
        forced_tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        target = delegation_target(delegated_call)
        if target == "medication_agent":
            return await self.medication_agent.run(
                trace_id,
                request_payload,
                forced_tool_calls=forced_tool_calls,
            )
        if target == "nutrition_management_agent":
            return await self.nutrition_management_agent.run(trace_id, request_payload)
        if target == "nutrition_recommendation_agent":
            return await self.nutrition_recommendation_agent.run(trace_id, request_payload)
        raise ValueError(f"unsupported_delegation_target:{target or 'unknown'}")

    @staticmethod
    def _delegation_request_payload(
        state: MultiturnGraphState,
        delegated_call: dict[str, Any],
    ) -> dict[str, Any]:
        request_payload = state["request_payload"]
        if state.get("entry_mode") != "mutation_resolution":
            return request_payload

        arguments = delegated_call.get("arguments") if isinstance(delegated_call.get("arguments"), dict) else {}
        task = str(arguments.get("task") or "").strip()
        if not task:
            return request_payload

        scoped_payload = dict(request_payload)
        scoped_context = dict(request_payload.get("context") or {})
        scoped_context["supervisor_delegation"] = {
            "task": task,
            "reason": str(arguments.get("reason") or "").strip(),
            "original_user_message": request_payload.get("message"),
            "mutation_resolution": scoped_context.get("mutation_resolution"),
        }
        scoped_payload["message"] = task
        scoped_payload["context"] = scoped_context
        return scoped_payload

    @staticmethod
    def _delegation_tool_result(
        trace_id: str,
        delegated_call: dict[str, Any],
        delegated_response: AgentResponse,
        *,
        sequence: int,
    ) -> ToolCallResult:
        tool_name = str(delegated_call.get("name") or "delegated_agent")
        return ToolCallResult(
            tool_name=tool_name,
            status=(
                "confirmation_required"
                if delegated_response.structured_payload.get("mutation_confirmation_required") is True
                else "success"
            ),
            response={
                "specialist_agent": delegated_response.agent_name,
                "decision_type": delegated_response.decision_type,
                "human_summary": delegated_response.human_summary,
                "structured_payload": delegated_response.structured_payload,
            },
            idempotency_key=f"{trace_id}:{tool_name}:delegation:{sequence}",
        )

    def _final_response(self, state: MultiturnGraphState) -> dict[str, AgentResponse]:
        if state.get("delegated_responses"):
            return self._delegated_response(state)
        if state.get("direct_tool_calls"):
            return self._direct_tool_response(state)
        return self._direct_answer_response(state)

    @staticmethod
    def _direct_answer_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        output = state.get("current_model_output", {})
        ai_message = state["current_ai_message"]
        human_summary, final_answer_source = patient_summary_with_source(
            ai_message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="system_guidance",
                structured_payload={
                    "routing_mode": "direct_answer",
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "supervisor_tool_calls": [],
                    "observations": string_list(output.get("observations")),
                    "model_output": output,
                    "tool_calls": [],
                    "tool_results": [],
                    "tools_executed": False,
                    "final_answer_source": final_answer_source,
                    "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "iterations": 0,
                },
                human_summary=human_summary,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _async_continuation_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        continuation_type = state.get("continuation_type", "")
        pending_calls = state.get("pending_tool_calls", [])
        executed_calls = state.get("all_supervisor_tool_calls", [])
        results = state.get("all_supervisor_tool_results", [])
        all_calls = [*executed_calls, *pending_calls]
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="async_continuation_requested",
                structured_payload={
                    "routing_mode": "direct_async_continuation",
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "model_output": state.get("initial_model_output", {}),
                    "final_model_output": state.get("current_model_output", {}),
                    "tool_calls": all_calls,
                    "supervisor_tool_calls": all_calls,
                    "tool_results": [result.model_dump(mode="json") for result in results],
                    "tools_executed": bool(executed_calls),
                    "message_flow": _supervisor_message_flow(
                        state.get("tool_round_result_counts", []),
                        pending_tool_call=True,
                    ),
                    "async_continuation_required": True,
                    "async_continuation_type": continuation_type,
                    "policy_confirmation_required": has_deferred_policy_tool_call(pending_calls),
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "iterations": state.get("iterations", 0),
                },
                human_summary=async_continuation_summary(continuation_type),
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _max_iterations_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        executed_calls = state.get("all_supervisor_tool_calls", [])
        results = state.get("all_supervisor_tool_results", [])
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="tool_loop_limit_exceeded",
                structured_payload={
                    "routing_mode": "supervisor_max_iterations",
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "supervisor_tool_calls": executed_calls,
                    "pending_tool_calls": state.get("pending_tool_calls", []),
                    "model_output": state.get("initial_model_output", {}),
                    "final_model_output": state.get("current_model_output", {}),
                    **tool_calls_payload(executed_calls, results),
                    "message_flow": _supervisor_message_flow(
                        state.get("tool_round_result_counts", []),
                        pending_tool_call=True,
                    ),
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "iterations": state.get("iterations", 0),
                },
                human_summary=("요청 처리 중 도구 실행 한도에 도달했습니다. 요청을 나누어 다시 시도해 주세요."),
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _direct_tool_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        executed_calls = state.get("direct_tool_calls", [])
        results = state.get("direct_tool_results", [])
        all_tool_messages = state.get("all_tool_messages", [])
        initial_output = state.get("initial_model_output", {})
        final_output = state.get("current_model_output", {})
        final_ai_message = state["current_ai_message"]
        structured_payload = {
            "routing_mode": "direct_tool",
            "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
            "executed_by": MULTITURN_CHAT_AGENT_NAME,
            "supervisor_tool_calls": state.get("all_supervisor_tool_calls", []),
            "model_output": initial_output,
            **tool_calls_payload(executed_calls, results),
            "tool_messages": [{"name": message.name, "tool_call_id": message.tool_call_id, "content": message.content} for message in all_tool_messages],
            "final_model_output": final_output,
            "message_flow": _supervisor_message_flow(state.get("tool_round_result_counts", [])),
            "policy_confirmation_required": has_deferred_policy_tool_call(executed_calls),
            "agent_graph_mode": TOOL_LOOP_MODE,
            "tool_loop_mode": TOOL_LOOP_MODE,
            "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
            "finalization_mode": ITERATIVE_FINALIZATION_MODE,
            "iterations": state.get("iterations", 0),
        }
        fallback_summary = tool_result_summary(
            results,
            natural_chat_summary(initial_output) or "요청한 도구를 실행했습니다.",
        )
        final_summary = finalized_chat_summary(final_output)
        if final_summary:
            final_answer_source = str(final_output.get("fallback") or "model_output")
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
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                routing_mode=structured_payload["routing_mode"],
                fallback_source=final_answer_source,
                tool_names=[str(call.get("name") or "") for call in executed_calls],
            )
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="tool_call",
                structured_payload=structured_payload,
                human_summary=final_summary,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _delegated_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        delegated_responses = state.get("delegated_responses", [])
        delegation_calls = state.get("delegation_calls", [])
        last_response = delegated_responses[-1]
        last_call = delegation_calls[-1]
        final_ai_message = state["current_ai_message"]
        final_output = state.get("current_model_output", {})

        structured: dict[str, Any] = {}
        specialist_tool_calls: list[dict[str, Any]] = []
        specialist_tool_results: list[dict[str, Any]] = []
        specialist_tool_messages: list[dict[str, Any]] = []
        for response in delegated_responses:
            response_structured = response.structured_payload
            structured.update(response_structured)
            calls = response_structured.get("tool_calls")
            if isinstance(calls, list):
                specialist_tool_calls.extend(call for call in calls if isinstance(call, dict))
            results = response_structured.get("tool_results")
            if isinstance(results, list):
                specialist_tool_results.extend(result for result in results if isinstance(result, dict))
            messages = response_structured.get("tool_messages")
            if isinstance(messages, list):
                specialist_tool_messages.extend(message for message in messages if isinstance(message, dict))

        final_summary = finalized_chat_summary(final_output)
        if final_summary:
            final_answer_source = str(final_output.get("fallback") or "model_output")
        else:
            final_summary, final_answer_source = patient_summary_with_source(
                final_ai_message,
                last_response.human_summary,
                fallback_source="delegated_agent_summary",
            )

        response_decision_type = last_response.decision_type
        if response_decision_type != "async_continuation_requested" and any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in specialist_tool_calls):
            response_decision_type = "side_effect_assessment"

        all_supervisor_calls = state.get("all_supervisor_tool_calls", [])
        all_supervisor_results = state.get("all_supervisor_tool_results", [])
        delegation_results = [result for call, result in zip(all_supervisor_calls, all_supervisor_results, strict=True) if is_delegation_tool_call(call)]
        structured["routing_mode"] = "delegated_agent"
        structured["supervisor_agent"] = MULTITURN_CHAT_AGENT_NAME
        structured["specialist_agent"] = last_response.agent_name
        structured["executed_by"] = MULTITURN_CHAT_AGENT_NAME
        structured["delegated_agent"] = last_response.agent_name
        structured["delegated_agents"] = [response.agent_name for response in delegated_responses]
        structured["delegation_reason"] = delegation_reason(last_call)
        structured["delegation_reasons"] = [delegation_reason(call) for call in delegation_calls]
        structured["delegated_by"] = MULTITURN_CHAT_AGENT_NAME
        structured["supervisor_tool_calls"] = all_supervisor_calls
        structured["supervisor_tool_results"] = [result.model_dump(mode="json") for result in all_supervisor_results]
        structured["specialist_tool_calls"] = specialist_tool_calls
        structured["tool_calls"] = specialist_tool_calls
        structured["tool_results"] = specialist_tool_results
        structured["tools_executed"] = bool(specialist_tool_calls)
        if len(specialist_tool_calls) == 1:
            structured["tool_call"] = specialist_tool_calls[0]
        else:
            structured.pop("tool_call", None)
        structured["tool_messages"] = specialist_tool_messages
        if delegation_results:
            structured["delegation_tool_result"] = delegation_results[-1].model_dump(mode="json")
            structured["delegation_tool_results"] = [result.model_dump(mode="json") for result in delegation_results]
        structured["supervisor_model_output"] = state.get("initial_model_output", {})
        structured["supervisor_final_model_output"] = final_output
        structured["final_answer_source"] = final_answer_source
        structured["agent_graph_mode"] = TOOL_LOOP_MODE
        structured["tool_loop_mode"] = TOOL_LOOP_MODE
        structured["tool_execution_mode"] = ITERATIVE_TOOL_EXECUTION_MODE
        structured["finalization_mode"] = ITERATIVE_FINALIZATION_MODE
        structured["iterations"] = state.get("iterations", 0)
        structured["message_flow"] = _supervisor_message_flow(state.get("tool_round_result_counts", []))

        validation_errors = [error for response in delegated_responses for error in (response.validation_errors or [])]
        return {
            "response": AgentResponse(
                trace_id=last_response.trace_id,
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type=response_decision_type,
                structured_payload=structured,
                human_summary=final_summary,
                requires_conversation_alert=any(response.requires_conversation_alert for response in delegated_responses),
                validation_passed=all(response.validation_passed for response in delegated_responses),
                validation_errors=validation_errors,
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
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="system_guidance",
                error=safe_exception_summary(exc, limit=300),
                output_keys=sorted(str(key) for key in output.keys()),
            )
            raise AgentExecutionError(
                str(exc),
                error_type="llm_output_validation_failed",
                trace_id=trace_id,
                agent_name=MULTITURN_CHAT_AGENT_NAME,
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


def _supervisor_message_flow(
    tool_round_result_counts: list[int],
    *,
    pending_tool_call: bool = False,
) -> list[str]:
    flow = ["HumanMessage"]
    for result_count in tool_round_result_counts:
        flow.append("AIMessage(tool_calls)")
        flow.extend("ToolMessage" for _ in range(max(1, result_count)))
    if pending_tool_call:
        flow.append("AIMessage(tool_calls)")
    else:
        flow.append("AIMessage(final_answer)")
    return flow


def _mutation_confirmation_from_state(state: MultiturnGraphState) -> dict[str, Any]:
    for response in state.get("delegated_responses", []):
        proposal = response.structured_payload.get("mutation_confirmation")
        if isinstance(proposal, dict) and proposal:
            return proposal
    for result in state.get("all_supervisor_tool_results", []):
        response = result.response if isinstance(result.response, dict) else {}
        proposal = response.get("mutation_confirmation")
        if isinstance(proposal, dict) and proposal:
            return proposal
        specialist = response.get("structured_payload")
        if isinstance(specialist, dict):
            proposal = specialist.get("mutation_confirmation")
            if isinstance(proposal, dict) and proposal:
                return proposal
    return {}


def _specialist_tool_payloads(
    responses: list[AgentResponse],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for response in responses:
        structured = response.structured_payload
        response_calls = structured.get("tool_calls")
        if isinstance(response_calls, list):
            calls.extend(item for item in response_calls if isinstance(item, dict))
        response_results = structured.get("tool_results")
        if isinstance(response_results, list):
            results.extend(item for item in response_results if isinstance(item, dict))
        response_messages = structured.get("tool_messages")
        if isinstance(response_messages, list):
            messages.extend(item for item in response_messages if isinstance(item, dict))
    return calls, results, messages
