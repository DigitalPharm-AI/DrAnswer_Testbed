from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

ToolCallObservation = dict[str, Any]

_TOOL_CALL_COLLECTOR: ContextVar[
    list[ToolCallObservation] | None
] = ContextVar("agent_tool_call_collector", default=None)


@contextmanager
def capture_tool_calls(
    observations: list[ToolCallObservation] | None = None,
) -> Iterator[list[ToolCallObservation]]:
    """Collect Tool runtime evidence for one logical Agent run.

    Observations stay in process memory until the Agent Trace is persisted.
    They are not attached to ToolMessages or exposed to the model.
    """

    collector = observations if observations is not None else []
    token = _TOOL_CALL_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _TOOL_CALL_COLLECTOR.reset(token)


def record_tool_call(
    *,
    trace_id: str,
    source_event_type: str,
    tool_index: int,
    tool_call_origin: str,
    routing: dict[str, Any],
    call: dict[str, Any],
    result: dict[str, Any],
    started_at: datetime,
    completed_at: datetime,
    latency_ms: int,
) -> None:
    collector = _TOOL_CALL_COLLECTOR.get()
    if collector is None:
        return
    collector.append(
        {
            "observation_id": uuid.uuid4().hex,
            "trace_id": trace_id,
            "source_event_type": source_event_type,
            "tool_index": max(0, int(tool_index)),
            "tool_call_origin": tool_call_origin,
            "routing": dict(routing),
            "call": dict(call),
            "result": dict(result),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "latency_ms": max(0, int(latency_ms)),
        }
    )
