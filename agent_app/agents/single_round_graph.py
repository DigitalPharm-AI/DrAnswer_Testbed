from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

from langchain_core.messages import SystemMessage
from langgraph.graph import END, START, StateGraph

from agent_app import trace_logging
from agent_app.llm.messages import (
    ai_message_from_tool_calls,
    build_chat_messages,
    langchain_tools_from_catalog,
    model_output_from_ai_message,
    tool_calls_from_ai_message,
    tool_messages_from_results,
)
from agent_app.errors import AgentExecutionError
from agent_app.llm.generation import agent_error
from agent_app.llm.validation import validate_llm_output
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.policy import normalize_policy_tool_calls
from agent_app.tools.runtime import ToolRuntime
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse

AGENT_GRAPH_MODE = "langgraph_state_graph"
SINGLE_ROUND_TOOL_EXECUTION_MODE = "single_round"
LLM_AFTER_TOOL_FINALIZATION_MODE = "llm_after_tool_result"


class SingleRoundAgentGraphState(TypedDict, total=False):
    trace_id: str
    request_payload: dict[str, Any]
    messages: list[Any]
    decision_ai_message: Any
    decision_model_output: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    executed_calls: list[dict[str, Any]]
    tool_results: list[Any]
    tool_messages: list[Any]
    final_ai_message: Any
    final_model_output: dict[str, Any]
    response: AgentResponse


ResponseBuilder = Callable[[SingleRoundAgentGraphState], AgentResponse]


class SingleRoundToolAgentGraph:
    def __init__(
        self,
        *,
        provider: BaseLLMProvider,
        tool_runtime: ToolRuntime,
        agent_name: str,
        validation_decision_type: str,
        source_event_type: str,
        prompt: str,
        final_prompt: str,
        response_mode: str,
        tool_names: tuple[str, ...],
        response_builder: ResponseBuilder,
    ) -> None:
        self.provider = provider
        self.tool_runtime = tool_runtime
        self.agent_name = agent_name
        self.validation_decision_type = validation_decision_type
        self.source_event_type = source_event_type
        self.prompt = prompt
        self.final_prompt = final_prompt
        self.response_mode = response_mode
        self.tool_names = tool_names
        self.response_builder = response_builder
        self.graph = self._build_graph()

    async def invoke(self, trace_id: str, request_payload: dict[str, Any]) -> AgentResponse:
        try:
            final_state = await self.graph.ainvoke(
                {
                    "trace_id": trace_id,
                    "request_payload": request_payload,
                    "executed_calls": [],
                    "tool_results": [],
                    "tool_messages": [],
                }
            )
            return final_state["response"]
        except Exception as exc:
            raise agent_error(trace_id, self.agent_name, self.validation_decision_type, exc) from exc

    def _build_graph(self):
        graph = StateGraph(SingleRoundAgentGraphState)
        graph.add_node("decision_llm", self._decision_llm)
        graph.add_node("tool_node", self._tool_node)
        graph.add_node("final_llm", self._final_llm)
        graph.add_node("response_node", self._response_node)
        graph.add_edge(START, "decision_llm")
        graph.add_conditional_edges(
            "decision_llm",
            self._route_after_decision,
            {"tool_node": "tool_node", "response_node": "response_node"},
        )
        graph.add_edge("tool_node", "final_llm")
        graph.add_edge("final_llm", "response_node")
        graph.add_edge("response_node", END)
        return graph.compile()

    async def _decision_llm(self, state: SingleRoundAgentGraphState) -> dict[str, Any]:
        request_payload = state["request_payload"]
        catalog_tools = ToolCatalog.tools_for(*self.tool_names)
        messages = build_chat_messages(
            self.prompt,
            {
                **request_payload,
                "response_mode": self.response_mode,
                "decision_type": self.validation_decision_type,
                "available_tools": catalog_tools,
            },
        )
        bound_model = self.provider.chat_model().bind_tools(langchain_tools_from_catalog(catalog_tools))
        ai_message = await bound_model.ainvoke(messages)
        model_output = model_output_from_ai_message(ai_message)
        tool_calls = normalize_policy_tool_calls(
            tool_calls_from_ai_message(ai_message),
            source_event_type=self.source_event_type,
        )
        validation_output = _model_output_with_tool_calls(model_output, tool_calls)
        self._validate_output(
            state["trace_id"],
            validation_output,
            request_payload,
            allow_tool_only=bool(tool_calls),
        )
        if tool_calls:
            ai_message = ai_message_from_tool_calls(
                tool_calls,
                content=str(ai_message.content or ""),
                model_output=validation_output,
            )
        return {
            "messages": messages,
            "decision_ai_message": ai_message,
            "decision_model_output": validation_output,
            "pending_tool_calls": tool_calls,
        }

    @staticmethod
    def _route_after_decision(state: SingleRoundAgentGraphState) -> str:
        return "tool_node" if state.get("pending_tool_calls") else "response_node"

    async def _tool_node(self, state: SingleRoundAgentGraphState) -> dict[str, Any]:
        tool_calls = state.get("pending_tool_calls", [])
        ai_message = state["decision_ai_message"]
        model_output = state.get("decision_model_output", {})
        executed_calls, results = await self.tool_runtime.execute(
            tool_calls,
            trace_id=state["trace_id"],
            source_event_type=self.source_event_type,
            payload=state["request_payload"],
            routing_context={
                "routing_mode": "event_tool",
                "executed_by": self.agent_name,
                "agent_graph_mode": AGENT_GRAPH_MODE,
                "tool_execution_mode": SINGLE_ROUND_TOOL_EXECUTION_MODE,
                "tool_names": [str(call.get("name") or "") for call in tool_calls],
            },
        )
        if len(results) != len(executed_calls):
            raise RuntimeError("single_round_tool_execution_result_count_mismatch")
        executed_ai_message = ai_message_from_tool_calls(
            executed_calls,
            content=str(ai_message.content or ""),
            model_output=model_output,
        )
        tool_messages = tool_messages_from_results(executed_ai_message, executed_calls, results)
        return {
            "messages": [*state["messages"], executed_ai_message, *tool_messages],
            "executed_calls": executed_calls,
            "tool_results": results,
            "tool_messages": tool_messages,
            "pending_tool_calls": [],
        }

    async def _final_llm(self, state: SingleRoundAgentGraphState) -> dict[str, Any]:
        messages = state["messages"]
        final_messages = [SystemMessage(content=self.final_prompt), *messages[1:]]
        final_ai_message = await self.provider.chat_model().ainvoke(final_messages)
        if tool_calls_from_ai_message(final_ai_message):
            raise RuntimeError("single_round_finalizer_returned_tool_calls")
        final_model_output = model_output_from_ai_message(final_ai_message)
        self._validate_output(
            state["trace_id"],
            final_model_output,
            state["request_payload"],
        )
        return {
            "final_ai_message": final_ai_message,
            "final_model_output": final_model_output,
        }

    def _validate_output(
        self,
        trace_id: str,
        output: dict[str, Any],
        payload: dict[str, Any],
        *,
        allow_tool_only: bool = False,
    ) -> None:
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
    def _response_node(self, state: SingleRoundAgentGraphState) -> dict[str, AgentResponse]:
        return {"response": self.response_builder(state)}


def _model_output_with_tool_calls(
    model_output: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    merged = dict(model_output)
    if tool_calls:
        merged.pop("tool_call", None)
        merged["tool_calls"] = tool_calls
    return merged


def single_round_message_flow(tool_result_count: int) -> list[str]:
    if tool_result_count <= 0:
        return ["HumanMessage", "AIMessage(final_answer)"]
    return [
        "HumanMessage",
        "AIMessage(tool_calls)",
        *["ToolMessage" for _ in range(tool_result_count)],
        "AIMessage(final_answer)",
    ]
