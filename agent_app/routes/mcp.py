from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from agent_app.security import require_internal_api_token
from agent_app.tools.mcp_server import AgentMcpToolServer

router = APIRouter()

_mcp_server_getter: Callable[[], AgentMcpToolServer] | None = None


def configure_mcp_server(getter: Callable[[], AgentMcpToolServer]) -> None:
    global _mcp_server_getter
    _mcp_server_getter = getter


def _mcp_server() -> AgentMcpToolServer:
    if _mcp_server_getter is None:
        raise RuntimeError("agent_mcp_server_not_configured")
    return _mcp_server_getter()


@router.post("/agent/mcp", dependencies=[Depends(require_internal_api_token)])
async def agent_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    context_payload = meta.get("payload") if isinstance(meta.get("payload"), dict) else {}
    source_event_type = meta.get("source_event_type") or params.get("source_event_type") or "mcp"
    return await _mcp_server().handle_json_rpc(
        payload,
        trace_id=str(meta.get("trace_id") or f"mcp:{uuid.uuid4().hex}"),
        source_event_type=str(source_event_type),
        payload=context_payload,
    )
