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
    model_output_with_tool_calls,
    public_text_from_ai_message,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from shared.tool_confirmations import ConfirmationActionRegistry
from agent_app.orchestration.continuation import continuation_type
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import PROMPT_VERSION_ID
from agent_app.observability.model_calls import traced_model_ainvoke
from agent_app.llm.validation import validate_llm_output
from agent_app.providers.base import BaseLLMProvider
from shared.tool_catalog import ToolCatalog
from shared.tool_names import REQUEST_RECORD_APPROVAL, UPDATE_MEDICATION_DOSE_EVENT_STATUS
from agent_app.tools.policy import has_deferred_policy_tool_call, normalize_policy_tool_calls
from agent_app.tools.policy_gate import (
    ToolCallContext,
    ToolCallOrigin,
    tool_call_fingerprint,
)
from agent_app.tools.results import public_tool_calls, tool_calls_payload
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
    seeded_tool_calls: list[dict[str, Any]] | None
    seeded_tool_origin: ToolCallOrigin
    pending_tool_call_origin: ToolCallOrigin
    bound_model: Any
    messages: list[Any]
    current_ai_message: Any
    current_model_output: dict[str, Any]
    current_final_text: str
    initial_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    all_executed_calls: list[dict[str, Any]]
    all_results: list[Any]
    all_tool_messages: list[Any]
    iterations: int
    continuation_type: str
    confirmation_required: bool
    seeded_tool_calls_consumed: bool
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
        defer_tool_continuation: bool = True,
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
        self.defer_tool_continuation = defer_tool_continuation
        self.validation_decision_type = validation_decision_type
        self.response_builder = response_builder
        self.requires_conversation_alert = requires_conversation_alert
        self.max_iterations = max_iterations
        self.graph = self._build_graph()

    async def invoke(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
    ) -> AgentResponse:
        return await self._invoke(
            trace_id,
            request_payload,
            seeded_tool_calls=None,
            seeded_tool_origin=ToolCallOrigin.MODEL,
        )

    async def continue_with_tool_calls(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_calls: list[dict[str, Any]],
        origin: ToolCallOrigin = ToolCallOrigin.CLINICAL_CONTINUATION,
    ) -> AgentResponse:
        if origin is ToolCallOrigin.APPROVED_WRITE:
            raise ValueError(
                "approved_write must use execute_approved_write"
            )
        return await self._invoke(
            trace_id,
            request_payload,
            seeded_tool_calls=tool_calls,
            seeded_tool_origin=origin,
        )

    async def execute_approved_write(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        tool_call: dict[str, Any],
    ) -> AgentResponse:
        payload = dict(request_payload)
        executed_calls, results = await self.tool_runtime.execute(
            [tool_call],
            trace_id=trace_id,
            source_event_type=self.source_event_type,
            payload=payload,
            call_context=ToolCallContext(
                origin=ToolCallOrigin.APPROVED_WRITE
            ),
            routing_context={
                "routing_mode": "approved_write",
                "executed_by": self.agent_name,
                "specialist_agent": self.agent_name,
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
                agent_name=self.agent_name,
                decision_type=self.decision_type,
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
                agent_name=self.agent_name,
                decision_type=self.decision_type,
            )

        finalizer_messages = build_chat_messages(
            self.prompt,
            {
                **payload,
                "response_mode": "approved_write_finalization",
                "decision_type": self.decision_type,
                "approved_write_result": result.model_dump(mode="json"),
            },
        )
        ai_message = await traced_model_ainvoke(
            self.provider.chat_model(),
            finalizer_messages,
            name=f"{self.agent_name}.approved_write_finalizer",
            prompt_version_id=PROMPT_VERSION_ID,
        )
        followup_calls = normalize_policy_tool_calls(
            tool_calls_from_ai_message(ai_message),
            source_event_type=self.source_event_type,
        )
        if followup_calls:
            raise AgentExecutionError(
                "approved_write_followup_tool_forbidden",
                error_type="approved_write_followup_tool_forbidden",
                trace_id=trace_id,
                agent_name=self.agent_name,
                decision_type=self.decision_type,
            )
        final_text = public_text_from_ai_message(ai_message)
        if not final_text:
            raise AgentExecutionError(
                "llm_final_answer_missing",
                error_type="llm_final_answer_missing",
                trace_id=trace_id,
                agent_name=self.agent_name,
                decision_type=self.decision_type,
            )
        return AgentResponse(
            trace_id=trace_id,
            agent_name=self.agent_name,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type=self.decision_type,
            structured_payload={
                "routing_mode": "approved_write",
                "tool_loop_mode": "direct_approved_write",
                "agent_graph_mode": TOOL_LOOP_MODE,
                "executed_by": self.agent_name,
                "specialist_agent": self.agent_name,
                "specialist_tool_calls": public_tool_calls(
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

    async def _invoke(
        self,
        trace_id: str,
        request_payload: dict[str, Any],
        *,
        seeded_tool_calls: list[dict[str, Any]] | None,
        seeded_tool_origin: ToolCallOrigin,
    ) -> AgentResponse:
        payload = dict(request_payload)
        catalog_tools = ToolCatalog.model_tools_for(*self.tool_names)
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
                "seeded_tool_calls": (
                    list(seeded_tool_calls)
                    if seeded_tool_calls is not None
                    else None
                ),
                "seeded_tool_origin": seeded_tool_origin,
                "pending_tool_call_origin": ToolCallOrigin.MODEL,
                "messages": initial_messages,
                "all_executed_calls": [],
                "all_results": [],
                "all_tool_messages": [],
                "iterations": 0,
                "seeded_tool_calls_consumed": False,
            }
        )
        return final_state["response"]

    def _build_graph(self):
        graph = StateGraph(ToolChatGraphState)
        graph.add_node("prepare_model", self._prepare_model)
        graph.add_node("llm_call", self._llm_call)
        graph.add_node("tool_node", self._tool_node)
        graph.add_node("final_response", self._final_response)
        graph.add_node(
            "continuation_required_response",
            self._continuation_required_response,
        )
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
                "continuation_required_response": "continuation_required_response",
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
        graph.add_edge("continuation_required_response", END)
        graph.add_edge("confirmation_response", END)
        graph.add_edge("max_iterations_response", END)
        return graph.compile()

    def _prepare_model(self, state: ToolChatGraphState) -> dict[str, Any]:
        # Keep the tool schema attached when ToolMessages are returned to
        # Bedrock. Approved writes use a dedicated seeded entry and the policy
        # guard below rejects any model-requested follow-up Tool call.
        catalog_tools = ToolCatalog.model_tools_for(*self.tool_names)
        bound_model = self.provider.chat_model().bind_tools(
            langchain_tools_from_catalog(catalog_tools)
        )
        return {"bound_model": bound_model}

    async def _llm_call(self, state: ToolChatGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        seeded_tool_calls = state.get("seeded_tool_calls")
        seeded_tool_origin = state.get(
            "seeded_tool_origin",
            ToolCallOrigin.MODEL,
        )
        if (
            seeded_tool_calls
            and not state.get("seeded_tool_calls_consumed", False)
        ):
            tool_calls = normalize_policy_tool_calls(
                seeded_tool_calls,
                source_event_type=self.source_event_type,
            )
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
            ai_message = await traced_model_ainvoke(
                state["bound_model"],
                state["messages"],
                name=f"{self.agent_name}.tool_loop",
                prompt_version_id=PROMPT_VERSION_ID,
            )
            tool_calls = normalize_policy_tool_calls(
                tool_calls_from_ai_message(ai_message),
                source_event_type=self.source_event_type,
            )
            if tool_calls:
                output = model_output_with_tool_calls({}, tool_calls)
                final_text = ""
            elif self.response_builder is not None:
                # Event-specific structured agents keep their explicit JSON
                # contract. General medication/nutrition chat does not use it.
                output = model_output_from_ai_message(ai_message)
                final_text = ""
            else:
                output = {}
                final_text = public_text_from_ai_message(ai_message)
                if not final_text:
                    raise AgentExecutionError(
                        "llm_final_answer_missing",
                        error_type="llm_final_answer_missing",
                        trace_id=state["trace_id"],
                        agent_name=self.agent_name,
                        decision_type=self.decision_type,
                    )
            seeded_tool_calls_consumed = state.get(
                "seeded_tool_calls_consumed",
                False,
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
                    agent_name=self.agent_name,
                    decision_type=self.decision_type,
                )

        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=ai_message.content,
                model_output=output,
            )

        validation_output = model_output_with_tool_calls(output, tool_calls)
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
            "current_final_text": final_text,
            "pending_tool_calls": tool_calls,
            "continuation_type": continuation_type(tool_calls),
            "seeded_tool_calls_consumed": seeded_tool_calls_consumed,
            "pending_tool_call_origin": pending_tool_call_origin,
        }
        if "initial_model_output" not in state:
            updates["initial_model_output"] = dict(output)
        return updates

    def _route_after_llm(self, state: ToolChatGraphState) -> str:
        tool_calls = state.get("pending_tool_calls", [])
        continuation_type = state.get("continuation_type", "")
        if (
            self.defer_tool_continuation
            and continuation_type
            and state.get("seeded_tool_calls") is None
            and not any(str(call.get("name") or "") == UPDATE_MEDICATION_DOSE_EVENT_STATUS for call in tool_calls)
        ):
            return "continuation_required_response"
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
            call_context=ToolCallContext(
                origin=state.get(
                    "pending_tool_call_origin",
                    ToolCallOrigin.MODEL,
                ),
                prior_call_fingerprints=frozenset(
                    tool_call_fingerprint(call)
                    for call in state.get("all_executed_calls", [])
                ),
            ),
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
        executed_ai_message = (
            ai_message_from_tool_calls(
                executed_calls,
                content=ai_message.content,
                model_output=output,
            )
            if executed_calls
            else ai_message
        )
        tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
        confirmation_required = any(result.status == "confirmation_required" for result in results)
        if not confirmation_required:
            failed_mutation = next(
                (
                    result
                    for result in results
                    if (
                        result.tool_name == REQUEST_RECORD_APPROVAL
                        or ConfirmationActionRegistry.requires_confirmation(result.tool_name)
                    )
                    and result.status not in {"success", "confirmation_required"}
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
        proposal_display = (
            proposal.get("display")
            if isinstance(proposal.get("display"), dict)
            else {}
        )
        confirmation_summary = str(
            proposal_display.get("question")
            or "요청한 내용을 저장할까요?"
        ).strip()
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
                human_summary=confirmation_summary,
                requires_conversation_alert=False,
            )
        }

    def _continuation_required_response(
        self,
        state: ToolChatGraphState,
    ) -> dict[str, AgentResponse]:
        continuation_type = state.get("continuation_type", "")
        tool_calls = state.get("pending_tool_calls", [])
        all_executed_calls = state.get("all_executed_calls", [])
        all_results = state.get("all_results", [])
        return {
            "response": AgentResponse(
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                prompt_version_id=PROMPT_VERSION_ID,
                decision_type="continuation_required",
                structured_payload={
                    "routing_mode": "specialist_tool_continuation",
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
                    "continuation_required": True,
                    "continuation_type": continuation_type,
                    "policy_confirmation_required": has_deferred_policy_tool_call(tool_calls),
                },
                human_summary="",
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
        final_summary = state.get("current_final_text", "").strip()
        if not final_summary:
            raise AgentExecutionError(
                "llm_final_answer_missing",
                error_type="llm_final_answer_missing",
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                decision_type=self.decision_type,
            )
        structured_payload["final_answer_source"] = "agent_loop_final_text"
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
        output = state.get("current_model_output", {})
        human_summary = state.get("current_final_text", "").strip()
        if not human_summary:
            raise AgentExecutionError(
                "llm_final_answer_missing",
                error_type="llm_final_answer_missing",
                trace_id=state["trace_id"],
                agent_name=self.agent_name,
                decision_type=self.decision_type,
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
                "model_output": output,
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "final_answer_source": "agent_loop_final_text",
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
