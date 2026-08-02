from __future__ import annotations

import asyncio

import pytest

from agent_app.errors import AgentExecutionError, public_processing_error
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.tools.budget import tool_execution_budget_scope
from tests.support.agent_scenarios import (
    NativeDelegatingMedicationProvider,
    NativeFakeToolExecutor,
    build_taken_chat_request,
)


def test_supervisor_and_specialist_share_one_tool_execution_budget() -> None:
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(
        NativeDelegatingMedicationProvider(),
        executor,
    )

    response = asyncio.run(
        orchestrator.invoke(
            "multiturn_chat",
            build_taken_chat_request().model_dump(mode="json"),
            trace_id="trace-budget-shared",
        )
    )

    budget = response.structured_payload["tool_execution_budget"]
    assert budget["used_tool_calls"] == 2
    assert budget["remaining_tool_calls"] == budget["max_tool_calls"] - 2
    assert budget["used_delegations"] == 1
    assert budget["remaining_delegations"] == budget["max_delegations"] - 1
    assert len(executor.calls) == 1


def test_total_budget_stops_nested_tool_before_executor_call() -> None:
    executor = NativeFakeToolExecutor()
    orchestrator = AgentLangGraphNativeOrchestrator(
        NativeDelegatingMedicationProvider(),
        executor,
    )

    async def invoke() -> None:
        with tool_execution_budget_scope(
            max_tool_calls=1,
            max_calls_per_turn=1,
            max_delegations=1,
        ):
            await orchestrator.invoke(
                "multiturn_chat",
                build_taken_chat_request().model_dump(mode="json"),
                trace_id="trace-budget-exceeded",
            )

    with pytest.raises(AgentExecutionError) as captured:
        asyncio.run(invoke())

    assert captured.value.error_type == "tool_execution_budget_exceeded"
    assert public_processing_error(captured.value).code == ("TOOL_EXECUTION_BUDGET_EXCEEDED")
    assert executor.calls == []


def test_per_turn_budget_rejects_oversized_model_plan() -> None:
    with tool_execution_budget_scope(
        max_tool_calls=10,
        max_calls_per_turn=2,
        max_delegations=4,
    ) as budget:
        with pytest.raises(AgentExecutionError) as captured:
            budget.validate_turn_plan(
                [{"name": "one"}, {"name": "two"}, {"name": "three"}],
                trace_id="trace-budget-turn",
                agent_name="test_agent",
                decision_type="tool_call",
            )

    assert captured.value.error_type == "tool_execution_budget_exceeded"


def test_concurrent_requests_have_isolated_tool_execution_budgets() -> None:
    async def consume(trace_id: str) -> dict[str, int]:
        with tool_execution_budget_scope(
            max_tool_calls=1,
            max_calls_per_turn=1,
            max_delegations=1,
        ) as budget:
            await asyncio.sleep(0)
            budget.consume_tool_call(
                "isolated_tool",
                trace_id=trace_id,
                agent_name="test_agent",
                decision_type="tool_call",
            )
            await asyncio.sleep(0)
            return budget.snapshot()

    async def consume_both() -> tuple[dict[str, int], dict[str, int]]:
        first, second = await asyncio.gather(
            consume("trace-budget-first"),
            consume("trace-budget-second"),
        )
        return first, second

    first, second = asyncio.run(consume_both())

    assert first["used_tool_calls"] == 1
    assert second["used_tool_calls"] == 1
    assert first["remaining_tool_calls"] == 0
    assert second["remaining_tool_calls"] == 0
