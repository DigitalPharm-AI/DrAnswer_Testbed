from __future__ import annotations

from time import perf_counter
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agent_app import trace_logging
from agent_app.orchestration.delegation import (
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
from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    model_output_with_tool_calls,
    patient_summary_with_source,
    public_text_from_ai_message,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.orchestration.continuation import continuation_type
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID, agent_error
from agent_app.observability.model_calls import traced_model_ainvoke
from agent_app.llm.validation import validate_llm_output, validate_mutation_confirmation_reply_output
from agent_app.llm.prompts import (
    multiturn_chat_prompt,
    mutation_confirmation_prompt,
    mutation_confirmation_reply_prompt,
    mutation_resolution_prompt,
)
from agent_app.providers.base import BaseLLMProvider
from agent_app.llm.responses import finalized_chat_summary, natural_chat_summary
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    DELEGATE_TO_MEDICATION_AGENT,
    GET_NOTIFICATION_POLICIES,
    RECORD_APPROVAL_ACTIONS,
    REQUEST_RECORD_APPROVAL,
    SIDE_EFFECT_TOOLS,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from shared.tool_permissions import POLICY_TOOLS
from agent_app.tools.policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tools.policy_gate import (
    ToolCallContext,
    ToolCallOrigin,
    tool_call_fingerprint,
)
from agent_app.tools.results import public_tool_calls, tool_calls_payload
from agent_app.tools.runtime import ToolRuntime
from shared.chat_contracts import has_structured_ui_response
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse, MultiturnChatRequest, ToolCallResult

SUPERVISOR_DIRECT_TOOLS = tuple(
    sorted(
        {
            *POLICY_TOOLS,
            GET_NOTIFICATION_POLICIES,
            REQUEST_RECORD_APPROVAL,
            CHANGE_NOTIFICATION_POLICY,
        }
    )
)
MULTITURN_CHAT_AGENT_NAME = "multiturn_chat_agent"


def _required_llm_summary(
    message: Any,
    output: dict[str, Any],
    *,
    trace_id: str,
    decision_type: str,
    finalized: bool = False,
) -> tuple[str, str]:
    if finalized:
        summary = finalized_chat_summary(output)
        source = str(output.get("fallback") or "model_output")
        if not summary:
            summary, source = patient_summary_with_source(
                message,
                "",
                fallback_source="model_output",
            )
    else:
        summary, source = patient_summary_with_source(
            message,
            natural_chat_summary(output),
            fallback_source="model_output",
        )
    summary = summary.strip()
    if not summary:
        raise AgentExecutionError(
            "llm_final_answer_missing",
            error_type="llm_final_answer_missing",
            trace_id=trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            decision_type=decision_type,
        )
    return summary, source


def _normalize_mutation_confirmation_output(
    output: dict[str, Any],
) -> dict[str, Any]:
    """Normalize provider wording into the canonical confirmation message field."""

    normalized = dict(output)
    message = str(normalized.get("message") or "").strip()
    if not message:
        question = str(normalized.get("question") or "").strip()
        if question:
            normalized["message"] = question
        else:
            advice = str(normalized.get("advice") or "").strip()
            if advice:
                normalized["message"] = advice
    normalized.pop("question", None)
    normalized.pop("advice", None)
    return normalized


class MultiturnGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    context: dict[str, Any]
    messages: list[Any]
    bound_model: Any
    current_ai_message: Any
    current_model_output: dict[str, Any]
    current_final_text: str
    initial_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    continuation_type: str
    specialist_continuation_tool_calls: list[dict[str, Any]]
    seeded_tool_calls_consumed: bool
    seeded_tool_origin: ToolCallOrigin
    pending_tool_call_origin: ToolCallOrigin
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
            approved_action = context.get("approved_user_action")
            approved_action_name = (
                str(approved_action.get("action_name") or "")
                if isinstance(approved_action, dict)
                else ""
            )
            if (
                isinstance(approved_action, dict)
                and approved_action.get("status") == "confirmed"
                and approved_action_name in RECORD_APPROVAL_ACTIONS
            ):
                model_arguments = approved_action.get("arguments")
                if not isinstance(model_arguments, dict):
                    raise ValueError("approved_user_action_arguments_missing")
                approved_tool_call = {
                    "id": f"approved_{approved_action_name}",
                    "name": approved_action_name,
                    "arguments": model_arguments,
                }
                if approved_action_name == CHANGE_NOTIFICATION_POLICY:
                    return await self._execute_approved_supervisor_write(
                        trace_id,
                        request_payload,
                        approved_tool_call,
                    )
                else:
                    target_agent = (
                        self.medication_agent
                        if approved_action_name
                        in {
                            CREATE_MEDICATION_SIDE_EFFECT_RECORD,
                            UPDATE_MEDICATION_DOSE_EVENT_STATUS,
                        }
                        else self.nutrition_management_agent
                    )
                    specialist_response = (
                        await target_agent.execute_approved_write(
                            trace_id,
                            request_payload,
                            tool_call=approved_tool_call,
                        )
                    )
                    return specialist_response
            final_state = await self.graph.ainvoke(
                {
                    "trace_id": trace_id,
                    "request_payload": request_payload,
                    "context": context,
                    "specialist_continuation_tool_calls": [],
                    "seeded_tool_calls_consumed": False,
                    "seeded_tool_origin": ToolCallOrigin.MODEL,
                    "pending_tool_call_origin": ToolCallOrigin.MODEL,
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
                revision = final_state.get("context", {}).get("mutation_confirmation_revision")
                if reply_intent == "revise" and isinstance(revision, dict):
                    structured_payload["mutation_confirmation_revision"] = revision
                response = response.model_copy(update={"structured_payload": structured_payload})
            return response
        except Exception as exc:
            raise agent_error(trace_id, MULTITURN_CHAT_AGENT_NAME, "system_guidance", exc) from exc

    async def _execute_approved_supervisor_write(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        tool_call: dict[str, Any],
    ) -> AgentResponse:
        executed_calls, results = await self.tool_runtime.execute(
            [tool_call],
            trace_id=trace_id,
            source_event_type="multiturn_chat",
            payload=request_payload,
            call_context=ToolCallContext(
                origin=ToolCallOrigin.APPROVED_WRITE
            ),
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
                (
                    "approved_write_not_applied:"
                    f"{result.tool_name}:{result.status}"
                ),
                error_type="mutation_tool_not_applied",
                trace_id=trace_id,
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                decision_type="system_guidance",
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
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            finalizer_messages,
            name="multiturn_chat.approved_write_finalizer",
            prompt_version_id=PROMPT_VERSION_ID,
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
                "supervisor_tool_calls": public_tool_calls(
                    executed_calls
                ),
                **tool_calls_payload(executed_calls, results),
                "final_answer_source": (
                    "approved_write_finalizer"
                ),
                "message_flow": [
                    "ApprovedToolCall",
                    "ToolResult",
                    "AIMessage(final_answer)",
                ],
            },
            human_summary=final_text,
            requires_conversation_alert=False,
        )

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
        graph.add_node(
            "continuation_required_response",
            self._continuation_required_response,
        )
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
                "continuation_required_response": "continuation_required_response",
                "max_iterations_response": "max_iterations_response",
            },
        )
        graph.add_conditional_edges(
            "supervisor_tool_node",
            self._route_after_tool,
            {
                "supervisor_llm": "supervisor_llm",
                "confirmation_llm": "confirmation_llm",
                "final_response": "final_response",
            },
        )
        graph.add_edge("confirmation_llm", "confirmation_response")
        graph.add_edge("prepare_mutation_resolution_model", "supervisor_llm")
        graph.add_edge("final_response", END)
        graph.add_edge("mutation_resolution_response", END)
        graph.add_edge("confirmation_reply_response", END)
        graph.add_edge("continuation_required_response", END)
        graph.add_edge("max_iterations_response", END)
        graph.add_edge("confirmation_response", END)
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
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            state["messages"],
            name="multiturn_chat.confirmation_reply",
            prompt_version_id=PROMPT_VERSION_ID,
        )
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
        if validated["intent"] in {"new_request", "revise"}:
            context = dict(state.get("context", {}))
            pending_confirmation = context.get("pending_mutation_confirmation")
            context.pop("pending_mutation_confirmation", None)
            request_metadata = dict(context.get("request_metadata") or {})
            request_metadata.pop("pending_mutation_confirmation", None)
            context["request_metadata"] = request_metadata
            context["pending_mutation_confirmation_reply_resolved"] = validated["intent"]
            if validated["intent"] == "revise" and isinstance(pending_confirmation, dict):
                context["mutation_confirmation_revision"] = {
                    "display": pending_confirmation.get("display", {}),
                    "original_request": pending_confirmation.get("original_request", ""),
                    "user_revision": state["request_payload"].get("message", ""),
                }
            request_payload = dict(state["request_payload"])
            request_payload["context"] = context
            updates["context"] = context
            updates["request_payload"] = request_payload
        return updates

    @staticmethod
    def _route_after_confirmation_reply(state: MultiturnGraphState) -> str:
        if state.get("confirmation_reply_intent") in {"new_request", "revise"}:
            return "prepare_model"
        return "confirmation_reply_response"

    @staticmethod
    def _confirmation_reply_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        output = state["confirmation_reply_model_output"]
        intent = str(output["intent"])
        human_summary = str(output["message"]).strip()
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
                    "final_answer_source": "confirmation_reply_llm",
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
        catalog_tools = [*ToolCatalog.model_tools_for(*SUPERVISOR_DIRECT_TOOLS), *delegation_tools_payload()]
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
        output = state.get("current_model_output", {})
        resolution = state.get("context", {}).get("mutation_resolution")
        supervisor_calls = state.get("all_supervisor_tool_calls", [])
        supervisor_results = state.get("all_supervisor_tool_results", [])
        delegated_responses = state.get("delegated_responses", [])
        human_summary = state.get("current_final_text", "").strip()
        final_answer_source = "agent_loop_final_text"
        specialist_payload: dict[str, Any] = {}
        for response in delegated_responses:
            specialist_payload.update(response.structured_payload)
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
                    **specialist_payload,
                    "routing_mode": routing_mode,
                    "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                    "executed_by": MULTITURN_CHAT_AGENT_NAME,
                    "mutation_resolution": resolution if isinstance(resolution, dict) else {},
                    "model_output": output,
                    "supervisor_tool_calls": supervisor_calls,
                    "supervisor_tool_results": [result.model_dump(mode="json") for result in supervisor_results],
                    "delegated_agents": delegated_agents,
                    "specialist_agent": delegated_responses[-1].agent_name if delegated_responses else "",
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
        catalog_tools = [*ToolCatalog.model_tools_for(*SUPERVISOR_DIRECT_TOOLS), *delegation_tools_payload()]
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
        continuation_tool_calls = context.get("continuation_tool_calls")
        seeded_tool_calls_consumed = state.get(
            "seeded_tool_calls_consumed",
            False,
        )
        specialist_continuation_tool_calls = state.get(
            "specialist_continuation_tool_calls",
            [],
        )
        seeded_tool_origin = state.get(
            "seeded_tool_origin",
            ToolCallOrigin.MODEL,
        )
        resolution_elapsed_ms: int | None = None

        if (
            context.get("execute_tool_continuation") is True
            and isinstance(continuation_tool_calls, list)
            and not seeded_tool_calls_consumed
        ):
            try:
                seeded_tool_origin = ToolCallOrigin(
                    str(
                        context.get("continuation_tool_origin")
                        or ToolCallOrigin.CLINICAL_CONTINUATION.value
                    )
                )
            except ValueError:
                seeded_tool_origin = ToolCallOrigin.CLINICAL_CONTINUATION
            tool_calls = normalize_policy_tool_calls(
                continuation_tool_calls,
                source_event_type="multiturn_chat",
            )
            if any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in tool_calls):
                specialist_continuation_tool_calls = tool_calls
                tool_calls = [
                    {
                        "name": DELEGATE_TO_MEDICATION_AGENT,
                        "arguments": {
                            "task": "Continue the deferred medication side-effect assessment.",
                            "reason": "medication_tool_continuation",
                        },
                    }
                ]
            output = model_output_with_tool_calls({}, tool_calls)
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content="",
                model_output=output,
            )
            final_text = ""
            seeded_tool_calls_consumed = True
            pending_tool_call_origin = seeded_tool_origin
        else:
            started = perf_counter()
            ai_message = await traced_model_ainvoke(
                state["bound_model"],
                state["messages"],
                name="multiturn_chat.supervisor",
                prompt_version_id=PROMPT_VERSION_ID,
            )
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
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type="multiturn_chat",
            )
            if tool_calls:
                output = model_output_with_tool_calls({}, tool_calls)
                final_text = ""
            else:
                output = {}
                final_text = public_text_from_ai_message(ai_message)
                if not final_text:
                    raise AgentExecutionError(
                        "llm_final_answer_missing",
                        error_type="llm_final_answer_missing",
                        trace_id=state["trace_id"],
                        agent_name=MULTITURN_CHAT_AGENT_NAME,
                        decision_type="system_guidance",
                    )
            pending_tool_call_origin = ToolCallOrigin.MODEL
            if (
                seeded_tool_origin is ToolCallOrigin.APPROVED_WRITE
                and seeded_tool_calls_consumed
                and tool_calls
            ):
                raise AgentExecutionError(
                    "approved_write_followup_tool_forbidden",
                    error_type="approved_write_followup_tool_forbidden",
                    trace_id=state["trace_id"],
                    agent_name=MULTITURN_CHAT_AGENT_NAME,
                    decision_type="system_guidance",
                )

        validation_output = model_output_with_tool_calls(output, tool_calls)
        if tool_calls:
            self._validate_output(
                state["trace_id"],
                validation_output,
                request_payload,
            )
        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=ai_message.content,
                model_output=validation_output,
            )

        updates: dict[str, Any] = {
            "current_ai_message": ai_message,
            "current_model_output": validation_output,
            "current_final_text": final_text,
            "pending_tool_calls": tool_calls,
            "continuation_type": continuation_type(tool_calls),
            "specialist_continuation_tool_calls": (
                specialist_continuation_tool_calls
            ),
            "seeded_tool_calls_consumed": seeded_tool_calls_consumed,
            "seeded_tool_origin": seeded_tool_origin,
            "pending_tool_call_origin": pending_tool_call_origin,
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
        if (
            continuation_type
            and state.get("context", {}).get("execute_tool_continuation") is not True
        ):
            return "continuation_required_response"
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
        specialist_continuation_tool_calls = state.get(
            "specialist_continuation_tool_calls",
            [],
        )
        prior_delegation_count = len(state.get("delegation_calls", []))

        for call_index, call in enumerate(pending_calls):
            if is_delegation_tool_call(call):
                delegation_payload = self._delegation_request_payload(state, call)
                delegated_response = await self._run_delegation(
                    state["trace_id"],
                    delegation_payload,
                    call,
                    continuation_tool_calls=(
                        specialist_continuation_tool_calls or None
                    ),
                )
                specialist_continuation_tool_calls = []
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
                call_context=ToolCallContext(
                    origin=state.get(
                        "pending_tool_call_origin",
                        ToolCallOrigin.MODEL,
                    ),
                    prior_call_fingerprints=frozenset(
                        tool_call_fingerprint(previous_call)
                        for previous_call in state.get(
                            "all_supervisor_tool_calls",
                            [],
                        )
                    ),
                ),
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
            content=ai_message.content,
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
            "specialist_continuation_tool_calls": (
                specialist_continuation_tool_calls
            ),
            "confirmation_required": any(result.status == "confirmation_required" for result in results),
        }

    @staticmethod
    def _route_after_tool(state: MultiturnGraphState) -> str:
        if state.get("confirmation_required"):
            return "confirmation_llm"
        delegated = state.get("delegated_responses", [])
        if delegated:
            last_response = delegated[-1]
            if (
                last_response.decision_type == "continuation_required"
                or has_structured_ui_response(last_response)
            ):
                return "final_response"
        return "supervisor_llm"

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
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            messages,
            name="multiturn_chat.mutation_confirmation",
            prompt_version_id=PROMPT_VERSION_ID,
        )
        native_tool_calls = tool_calls_from_ai_message(ai_message)
        output = _normalize_mutation_confirmation_output(
            model_output_from_ai_message(ai_message)
        )
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
        return {
            "confirmation_ai_message": ai_message,
            "confirmation_model_output": output,
        }

    @staticmethod
    def _confirmation_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        proposal = _mutation_confirmation_from_state(state)
        ai_message = state["confirmation_ai_message"]
        output = state.get("confirmation_model_output", {})
        human_summary, final_answer_source = _required_llm_summary(
            ai_message,
            output,
            trace_id=state["trace_id"],
            decision_type="mutation_confirmation_required",
            finalized=True,
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
        continuation_tool_calls: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        target = delegation_target(delegated_call)
        if target == "medication_agent":
            if continuation_tool_calls:
                return await self.medication_agent.continue_with_tool_calls(
                    trace_id,
                    request_payload,
                    tool_calls=continuation_tool_calls,
                )
            return await self.medication_agent.run(
                trace_id,
                request_payload,
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
        final_text = state.get("current_final_text", "").strip()
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
                    "model_output": output,
                    "tool_calls": [],
                    "tool_results": [],
                    "tools_executed": False,
                    "final_answer_source": "agent_loop_final_text",
                    "finalization_mode": ITERATIVE_FINALIZATION_MODE,
                    "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "iterations": 0,
                },
                human_summary=final_text,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _continuation_required_response(
        state: MultiturnGraphState,
    ) -> dict[str, AgentResponse]:
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
                decision_type="continuation_required",
                structured_payload={
                    "routing_mode": "direct_tool_continuation",
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
                    "continuation_required": True,
                    "continuation_type": continuation_type,
                    "policy_confirmation_required": has_deferred_policy_tool_call(pending_calls),
                    "agent_graph_mode": TOOL_LOOP_MODE,
                    "tool_loop_mode": TOOL_LOOP_MODE,
                    "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                    "iterations": state.get("iterations", 0),
                },
                human_summary="",
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
        final_text = state.get("current_final_text", "").strip()
        structured_payload["final_answer_source"] = "agent_loop_final_text"
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=MULTITURN_CHAT_AGENT_NAME,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="tool_call",
                structured_payload=structured_payload,
                human_summary=final_text,
                requires_conversation_alert=False,
            )
        }

    @staticmethod
    def _delegated_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
        delegated_responses = state.get("delegated_responses", [])
        delegation_calls = state.get("delegation_calls", [])
        last_response = delegated_responses[-1]
        last_call = delegation_calls[-1]
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

        response_decision_type = last_response.decision_type
        if response_decision_type == "continuation_required":
            final_summary = ""
            final_answer_source = "internal_control"
        else:
            final_summary = state.get("current_final_text", "").strip()
            if final_summary:
                final_answer_source = "agent_loop_final_text"
            elif has_structured_ui_response(last_response):
                final_summary = last_response.human_summary
                final_answer_source = "specialist_handoff"
            else:
                final_answer_source = "missing"

        if response_decision_type != "continuation_required" and any(str(call.get("name") or "") in SIDE_EFFECT_TOOLS for call in specialist_tool_calls):
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
