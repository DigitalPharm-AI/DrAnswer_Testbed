from __future__ import annotations

from time import perf_counter
from typing import Any
from uuid import uuid4

from langgraph.graph import END, StateGraph

from agent_app.agents import DailyPatternAgent, MissedDoseAgent, MultiturnChatAgent
from agent_app.errors import AgentExecutionError
from agent_app.integration.food_selection_payloads import (
    food_portion_input_request,
)
from agent_app.orchestration.state import AgentGraphState
from agent_app.providers.base import BaseLLMProvider
from agent_app.tools.budget import (
    response_with_tool_execution_budget,
    tool_execution_budget_scope,
)
from agent_app.tools.protocol import AgentToolExecutorProtocol
from agent_app.tools.runtime import ToolRuntime
from shared.schemas import AgentResponse
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    REQUEST_RECORD_APPROVAL,
)


class AgentLangGraphNativeOrchestrator:
    _ROUTES = {
        "daily_pattern": "daily_pattern",
        "missed_dose": "missed_dose",
        "multiturn_chat": "multiturn_chat",
    }

    def __init__(
        self,
        provider: BaseLLMProvider,
        tool_executor: AgentToolExecutorProtocol | None = None,
        *,
        nutrition_recommendation_enabled: bool = True,
    ) -> None:
        self.nutrition_recommendation_enabled = nutrition_recommendation_enabled
        self.provider = provider
        self.tool_runtime = ToolRuntime(tool_executor)
        self.daily_pattern_agent = DailyPatternAgent(provider, self.tool_runtime)
        self.missed_dose_agent = MissedDoseAgent(provider, self.tool_runtime)
        self.multiturn_chat_agent = MultiturnChatAgent(
            provider,
            self.tool_runtime,
            nutrition_recommendation_enabled=self.nutrition_recommendation_enabled,
        )
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

    async def invoke(
        self,
        request_kind: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> AgentResponse:
        with tool_execution_budget_scope() as budget:
            response = await self._invoke_without_budget(
                request_kind,
                payload,
                trace_id=trace_id,
            )
            return response_with_tool_execution_budget(response, budget)

    async def _invoke_without_budget(
        self,
        request_kind: str,
        payload: dict[str, Any],
        *,
        trace_id: str | None = None,
    ) -> AgentResponse:
        trace_id = trace_id or str(uuid4())
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

    async def continue_nutrition_food_selection(
        self,
        *,
        trace_id: str,
        payload: dict[str, Any],
        record_arguments: dict[str, Any],
        selection_id: str,
        origin_message_id: str,
        require_portion_input: bool = True,
    ) -> AgentResponse:
        with tool_execution_budget_scope() as budget:
            response = await self._continue_nutrition_food_selection_without_budget(
                trace_id=trace_id,
                payload=payload,
                record_arguments=record_arguments,
                selection_id=selection_id,
                origin_message_id=origin_message_id,
                require_portion_input=require_portion_input,
            )
            return response_with_tool_execution_budget(response, budget)

    async def _continue_nutrition_food_selection_without_budget(
        self,
        *,
        trace_id: str,
        payload: dict[str, Any],
        record_arguments: dict[str, Any],
        selection_id: str,
        origin_message_id: str,
        require_portion_input: bool = True,
    ) -> AgentResponse:
        """Continue a Backend-validated food card without another LLM lookup."""

        continuation_payload = dict(payload)
        if require_portion_input:
            continuation_context = dict(payload.get("context") or {})
            continuation_context["record_input_request"] = {
                "action_name": CREATE_NUTRITION_MEAL_RECORD,
                "missing_fields": ["foods[].portion"],
                "input_request": food_portion_input_request(list(record_arguments.get("foods") or [])),
            }
            continuation_payload["context"] = continuation_context

        response = await self.multiturn_chat_agent.nutrition_management_agent.continue_with_tool_calls(
            trace_id,
            continuation_payload,
            tool_calls=[
                {
                    "id": ("trusted_food_selection_approval"),
                    "name": REQUEST_RECORD_APPROVAL,
                    "arguments": {
                        "action_name": (CREATE_NUTRITION_MEAL_RECORD),
                        "record_arguments": record_arguments,
                    },
                }
            ],
        )
        response.structured_payload["selection_state_resolution"] = {
            "reason_code": ("TRUSTED_FOOD_SELECTION_STATE_REUSED"),
            "selection_id": selection_id,
            "origin_message_id": origin_message_id,
            "candidate_reused": True,
            "search_repeated": False,
        }
        response.structured_payload["routing_mode"] = "trusted_food_selection_continuation"
        response.structured_payload["final_answer_source"] = "deterministic_selection_state"
        return response
