from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError
from shared.settings import get_settings


@dataclass
class ToolExecutionBudget:
    max_tool_calls: int
    max_calls_per_turn: int
    max_delegations: int
    used_tool_calls: int = 0
    used_delegations: int = 0

    def validate_turn_plan(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        trace_id: str,
        agent_name: str,
        decision_type: str,
    ) -> None:
        call_count = len(tool_calls)
        if call_count <= self.max_calls_per_turn:
            return
        self._raise_exceeded(
            reason="tool_calls_per_turn",
            requested=call_count,
            limit=self.max_calls_per_turn,
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )

    def ensure_capacity(
        self,
        requested: int,
        *,
        trace_id: str,
        agent_name: str,
        decision_type: str,
    ) -> None:
        if requested <= self.remaining_tool_calls:
            return
        self._raise_exceeded(
            reason="total_tool_calls",
            requested=requested,
            limit=self.max_tool_calls,
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )

    def consume_tool_call(
        self,
        tool_name: str,
        *,
        trace_id: str,
        agent_name: str,
        decision_type: str,
        delegation: bool = False,
    ) -> None:
        self.ensure_capacity(
            1,
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )
        if delegation and self.used_delegations >= self.max_delegations:
            self._raise_exceeded(
                reason="delegations",
                requested=1,
                limit=self.max_delegations,
                trace_id=trace_id,
                agent_name=agent_name,
                decision_type=decision_type,
            )
        self.used_tool_calls += 1
        if delegation:
            self.used_delegations += 1
        trace_logging.log_info(
            "agent_tool_execution_budget_consumed",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
            tool_name=tool_name,
            delegation=delegation,
            budget=self.snapshot(),
        )

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.used_tool_calls)

    @property
    def remaining_delegations(self) -> int:
        return max(0, self.max_delegations - self.used_delegations)

    def snapshot(self) -> dict[str, int]:
        return {
            "max_tool_calls": self.max_tool_calls,
            "max_calls_per_turn": self.max_calls_per_turn,
            "used_tool_calls": self.used_tool_calls,
            "remaining_tool_calls": self.remaining_tool_calls,
            "max_delegations": self.max_delegations,
            "used_delegations": self.used_delegations,
            "remaining_delegations": self.remaining_delegations,
        }

    def _raise_exceeded(
        self,
        *,
        reason: str,
        requested: int,
        limit: int,
        trace_id: str,
        agent_name: str,
        decision_type: str,
    ) -> None:
        trace_logging.log_info(
            "agent_tool_execution_budget_exceeded",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
            reason=reason,
            requested=requested,
            limit=limit,
            budget=self.snapshot(),
        )
        raise AgentExecutionError(
            (f"tool_execution_budget_exceeded:{reason}:requested={requested}:limit={limit}:used={self.used_tool_calls}"),
            error_type="tool_execution_budget_exceeded",
            trace_id=trace_id,
            agent_name=agent_name,
            decision_type=decision_type,
        )


_CURRENT_TOOL_EXECUTION_BUDGET: ContextVar[ToolExecutionBudget | None] = ContextVar("agent_tool_execution_budget", default=None)


@contextmanager
def tool_execution_budget_scope(
    *,
    max_tool_calls: int | None = None,
    max_calls_per_turn: int | None = None,
    max_delegations: int | None = None,
) -> Iterator[ToolExecutionBudget]:
    existing = _CURRENT_TOOL_EXECUTION_BUDGET.get()
    if existing is not None:
        yield existing
        return

    settings = get_settings()
    budget = ToolExecutionBudget(
        max_tool_calls=(max_tool_calls if max_tool_calls is not None else settings.agent_tool_execution_limit),
        max_calls_per_turn=(max_calls_per_turn if max_calls_per_turn is not None else settings.agent_tool_calls_per_turn_limit),
        max_delegations=(max_delegations if max_delegations is not None else settings.agent_delegation_limit),
    )
    token = _CURRENT_TOOL_EXECUTION_BUDGET.set(budget)
    try:
        yield budget
    finally:
        _CURRENT_TOOL_EXECUTION_BUDGET.reset(token)


def current_tool_execution_budget() -> ToolExecutionBudget:
    budget = _CURRENT_TOOL_EXECUTION_BUDGET.get()
    if budget is None:
        raise RuntimeError("tool_execution_budget_scope_missing")
    return budget


def response_with_tool_execution_budget(
    response: Any,
    budget: ToolExecutionBudget,
) -> Any:
    structured = dict(response.structured_payload)
    structured["tool_execution_budget"] = budget.snapshot()
    return response.model_copy(update={"structured_payload": structured})
