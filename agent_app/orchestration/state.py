from __future__ import annotations

from typing import Any, TypedDict

from shared.schemas import AgentResponse


class AgentGraphState(TypedDict, total=False):
    trace_id: str
    request_kind: str
    payload: dict[str, Any]
    route: str
    response: AgentResponse
