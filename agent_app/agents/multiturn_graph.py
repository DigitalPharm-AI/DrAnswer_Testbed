from __future__ import annotations

from typing import Any, Protocol

from langgraph.graph import END, START, StateGraph

from agent_app.agents.multiturn_responses import (
    confirmation_reply_response,
    confirmation_response,
    continuation_required_response,
    final_response,
    max_iterations_response,
    mutation_resolution_response,
)
from agent_app.agents.multiturn_state import MultiturnGraphState
from agent_app.agents.tool_chat import AGENT_TOOL_LOOP_LIMIT
from agent_app.orchestration.delegation import delegation_target
from shared.chat_contracts import has_structured_ui_response


class MultiturnGraphNodes(Protocol):
    def _prepare_model(self, state: MultiturnGraphState) -> dict[str, Any]: ...

    async def _supervisor_llm(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _supervisor_direct_tool_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _medication_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _nutrition_management_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _nutrition_recommendation_specialist_subgraph_node(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    def _complete_supervisor_tool_round(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _confirmation_llm(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    def _prepare_confirmation_reply_model(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    async def _confirmation_reply_llm(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...

    def _prepare_mutation_resolution_model(
        self,
        state: MultiturnGraphState,
    ) -> dict[str, Any]: ...


def build_multiturn_graph(agent: MultiturnGraphNodes):
    graph = StateGraph(MultiturnGraphState)
    graph.add_node("entry_router", entry_router)
    graph.add_node("prepare_model", agent._prepare_model)
    graph.add_node("supervisor_llm", agent._supervisor_llm)
    graph.add_node(
        "supervisor_direct_tool",
        agent._supervisor_direct_tool_node,
    )
    graph.add_node(
        "medication_specialist_subgraph",
        agent._medication_specialist_subgraph_node,
    )
    graph.add_node(
        "nutrition_management_specialist_subgraph",
        agent._nutrition_management_specialist_subgraph_node,
    )
    graph.add_node(
        "nutrition_recommendation_specialist_subgraph",
        agent._nutrition_recommendation_specialist_subgraph_node,
    )
    graph.add_node(
        "complete_supervisor_tool_round",
        agent._complete_supervisor_tool_round,
    )
    graph.add_node("confirmation_llm", agent._confirmation_llm)
    graph.add_node("confirmation_response", confirmation_response)
    graph.add_node(
        "prepare_confirmation_reply_model",
        agent._prepare_confirmation_reply_model,
    )
    graph.add_node("confirmation_reply_llm", agent._confirmation_reply_llm)
    graph.add_node(
        "confirmation_reply_response",
        confirmation_reply_response,
    )
    graph.add_node(
        "prepare_mutation_resolution_model",
        agent._prepare_mutation_resolution_model,
    )
    graph.add_node(
        "mutation_resolution_response",
        mutation_resolution_response,
    )
    graph.add_node("final_response", final_response)
    graph.add_node(
        "continuation_required_response",
        continuation_required_response,
    )
    graph.add_node("max_iterations_response", max_iterations_response)

    graph.add_edge(START, "entry_router")
    graph.add_conditional_edges(
        "entry_router",
        route_after_entry,
        {
            "prepare_model": "prepare_model",
            "prepare_mutation_resolution_model": "prepare_mutation_resolution_model",
            "prepare_confirmation_reply_model": "prepare_confirmation_reply_model",
        },
    )
    graph.add_edge("prepare_confirmation_reply_model", "confirmation_reply_llm")
    graph.add_conditional_edges(
        "confirmation_reply_llm",
        route_after_confirmation_reply,
        {
            "prepare_model": "prepare_model",
            "confirmation_reply_response": "confirmation_reply_response",
        },
    )
    graph.add_edge("prepare_model", "supervisor_llm")
    graph.add_conditional_edges(
        "supervisor_llm",
        route_after_llm,
        {
            "supervisor_direct_tool": "supervisor_direct_tool",
            "medication_specialist_subgraph": "medication_specialist_subgraph",
            "nutrition_management_specialist_subgraph": "nutrition_management_specialist_subgraph",
            "nutrition_recommendation_specialist_subgraph": "nutrition_recommendation_specialist_subgraph",
            "final_response": "final_response",
            "mutation_resolution_response": "mutation_resolution_response",
            "continuation_required_response": "continuation_required_response",
            "max_iterations_response": "max_iterations_response",
        },
    )
    tool_dispatch_routes = {
        "supervisor_direct_tool": "supervisor_direct_tool",
        "medication_specialist_subgraph": "medication_specialist_subgraph",
        "nutrition_management_specialist_subgraph": "nutrition_management_specialist_subgraph",
        "nutrition_recommendation_specialist_subgraph": "nutrition_recommendation_specialist_subgraph",
        "complete_supervisor_tool_round": "complete_supervisor_tool_round",
    }
    for tool_node in (
        "supervisor_direct_tool",
        "medication_specialist_subgraph",
        "nutrition_management_specialist_subgraph",
        "nutrition_recommendation_specialist_subgraph",
    ):
        graph.add_conditional_edges(
            tool_node,
            route_after_tool_dispatch,
            tool_dispatch_routes,
        )
    graph.add_conditional_edges(
        "complete_supervisor_tool_round",
        route_after_tool,
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


def entry_router(state: MultiturnGraphState) -> dict[str, str]:
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


def route_after_entry(state: MultiturnGraphState) -> str:
    if state.get("entry_mode") == "mutation_resolution":
        return "prepare_mutation_resolution_model"
    if state.get("entry_mode") == "mutation_confirmation_reply":
        return "prepare_confirmation_reply_model"
    return "prepare_model"


def route_after_confirmation_reply(state: MultiturnGraphState) -> str:
    if state.get("confirmation_reply_intent") in {"new_request", "revise"}:
        return "prepare_model"
    return "confirmation_reply_response"


def route_after_llm(state: MultiturnGraphState) -> str:
    tool_calls = state.get("pending_tool_calls", [])
    continuation = state.get("continuation_type", "")
    if continuation and state.get("context", {}).get("execute_tool_continuation") is not True:
        return "continuation_required_response"
    if not tool_calls:
        if state.get("entry_mode") == "mutation_resolution":
            return "mutation_resolution_response"
        return "final_response"
    if state.get("iterations", 0) >= AGENT_TOOL_LOOP_LIMIT:
        return "max_iterations_response"
    return route_pending_tool_call(state)


def route_after_tool_dispatch(state: MultiturnGraphState) -> str:
    if state.get("round_confirmation_required"):
        return "complete_supervisor_tool_round"
    if state.get("tool_dispatch_index", 0) >= len(state.get("pending_tool_calls", [])):
        return "complete_supervisor_tool_round"
    return route_pending_tool_call(state)


def route_pending_tool_call(state: MultiturnGraphState) -> str:
    pending_calls = state.get("pending_tool_calls", [])
    index = state.get("tool_dispatch_index", 0)
    if index >= len(pending_calls):
        return "complete_supervisor_tool_round"
    target = delegation_target(pending_calls[index])
    if target == "medication_agent":
        return "medication_specialist_subgraph"
    if target == "nutrition_management_agent":
        return "nutrition_management_specialist_subgraph"
    if target == "nutrition_recommendation_agent":
        return "nutrition_recommendation_specialist_subgraph"
    return "supervisor_direct_tool"


def route_after_tool(state: MultiturnGraphState) -> str:
    if state.get("confirmation_required"):
        return "confirmation_llm"
    delegated = state.get("delegated_responses", [])
    if delegated:
        last_response = delegated[-1]
        if last_response.decision_type == "continuation_required" or has_structured_ui_response(last_response):
            return "final_response"
    return "supervisor_llm"
