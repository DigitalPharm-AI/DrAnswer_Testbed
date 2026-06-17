from __future__ import annotations

from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.graph import END, StateGraph

from agent_app.agents import DailyPatternAgent, MissedDoseAgent, MultiturnChatAgent
from agent_app.errors import AgentExecutionError
from agent_app.providers import BaseLLMProvider
from agent_app.state import AgentGraphState
from agent_app.tool_protocol import AgentToolExecutorProtocol
from agent_app.tool_runtime import ToolRuntime
from shared.schemas import AgentResponse


class AgentLangGraphNativeOrchestrator:
    _ROUTES = {
        "daily_pattern": "daily_pattern",
        "missed_dose": "missed_dose",
        "multiturn_chat": "multiturn_chat",
    }

    def __init__(self, provider: BaseLLMProvider, tool_executor: AgentToolExecutorProtocol | None = None) -> None:
        self.provider = provider
        self.tool_runtime = ToolRuntime(tool_executor)
        self.daily_pattern_agent = DailyPatternAgent(provider, self.tool_runtime)
        self.missed_dose_agent = MissedDoseAgent(provider, self.tool_runtime)
        self.multiturn_chat_agent = MultiturnChatAgent(provider, self.tool_runtime)
        self.graph = self._build_graph()

    def _build_graph(self):
        graph = StateGraph(AgentGraphState)
        graph.add_node("route_request", self._route_request)
        graph.add_node("daily_pattern", self._daily_pattern_node)
        graph.add_node("missed_dose", self._missed_dose_node)
        graph.add_node("multiturn_chat", self._multiturn_chat_node)
        graph.set_entry_point("route_request")
        graph.add_conditional_edges("route_request", self._route_key, self._ROUTES)
        for node_name in self._ROUTES.values():
            graph.add_edge(node_name, END)
        return graph.compile()

    @staticmethod
    def _route_key(state: AgentGraphState) -> str:
        return state["route"]

    def _route_request(self, state: AgentGraphState) -> dict[str, str]:
        request_kind = state["request_kind"]
        route = self._ROUTES.get(request_kind)
        if route is None:
            raise AgentExecutionError(
                f"Unsupported request kind: {request_kind}",
                error_type="unsupported_request_kind",
                trace_id=state["trace_id"],
                agent_name="ingress_router",
                decision_type="route_request",
            )
        return {"route": route}

    async def _daily_pattern_node(self, state: AgentGraphState) -> dict[str, AgentResponse]:
        return {"response": await self.daily_pattern_agent.run(state["trace_id"], state["payload"])}

    async def _missed_dose_node(self, state: AgentGraphState) -> dict[str, AgentResponse]:
        return {"response": await self.missed_dose_agent.run(state["trace_id"], state["payload"])}

    async def _multiturn_chat_node(self, state: AgentGraphState) -> dict[str, AgentResponse]:
        return {"response": await self.multiturn_chat_agent.run(state["trace_id"], state["payload"])}

    async def invoke(self, request_kind: str, payload: dict[str, Any]) -> AgentResponse:
        trace_id = str(uuid4())
        started = perf_counter()
        try:
            final_state = await self.graph.ainvoke(
                {
                    "trace_id": trace_id,
                    "request_kind": request_kind,
                    "payload": payload,
                }
            )
        except AgentExecutionError:
            raise
        except Exception as exc:
            raise AgentExecutionError(
                str(exc),
                error_type="agent_graph_failed",
                trace_id=trace_id,
                agent_name="agent_app",
                decision_type="graph_invoke",
            ) from exc
        response = final_state["response"]
        response.structured_payload.setdefault("elapsed_ms", round((perf_counter() - started) * 1000))
        return response
