from __future__ import annotations

from typing import Any

from agent_app.tools.mcp_server import AgentMcpToolServer
from agent_app.tools.protocol import (
    MCP_METHOD_TOOLS_CALL,
    mcp_call_params,
    mcp_json_rpc_request,
    mcp_result_from_json_rpc_response,
    tool_result_from_mcp_result,
)
from shared.schemas import ToolCallResult


class McpAgentToolExecutor:
    def __init__(self, *, server: AgentMcpToolServer | None = None, system_base_url: str | None = None, phr_base_url: str | None = None, timeout_seconds: float = 30.0) -> None:
        self.server = server or AgentMcpToolServer(system_base_url=system_base_url, phr_base_url=phr_base_url, timeout_seconds=timeout_seconds)

    async def execute_tool_call(self, tool_call: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> ToolCallResult:
        params = mcp_call_params(tool_call)
        request = mcp_json_rpc_request(
            MCP_METHOD_TOOLS_CALL,
            params,
            request_id=f"{trace_id}:{params['name'] or 'unknown'}",
        )
        response = await self.server.handle_json_rpc(request, trace_id=trace_id, source_event_type=source_event_type, payload=payload)
        result = mcp_result_from_json_rpc_response(response)
        return tool_result_from_mcp_result(params["name"] or "unknown", result)
