from __future__ import annotations

import json
from typing import Any, Protocol

from shared.tool_names import ALL_TOOL_NAMES
from shared.redaction import redact_for_logging, redacted_clinical_text_label
from shared.schemas import ToolCallResult

ALLOWED_TOOL_NAMES = set(ALL_TOOL_NAMES)

MCP_JSONRPC_VERSION = "2.0"
MCP_METHOD_TOOLS_LIST = "tools/list"
MCP_METHOD_TOOLS_CALL = "tools/call"


class AgentToolExecutorProtocol(Protocol):
    async def execute_tool_call(self, tool_call: dict[str, Any], *, trace_id: str, source_event_type: str, payload: dict[str, Any]) -> ToolCallResult:
        ...


def mcp_call_params(tool_call: dict[str, Any]) -> dict[str, Any]:
    params = {
        "name": str(tool_call.get("name") or ""),
        "arguments": tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {},
    }
    tool_call_id = str(tool_call.get("id") or "")
    if tool_call_id:
        params["tool_call_id"] = tool_call_id
    return params


def mcp_json_rpc_request(method: str, params: dict[str, Any] | None = None, *, request_id: str | int | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "jsonrpc": MCP_JSONRPC_VERSION,
        "method": method,
    }
    if request_id is not None:
        request["id"] = request_id
    if params is not None:
        request["params"] = params
    return request


def mcp_success_response(request_id: str | int | None, result: dict[str, Any]) -> dict[str, Any]:
    response: dict[str, Any] = {
        "jsonrpc": MCP_JSONRPC_VERSION,
        "result": result,
    }
    if request_id is not None:
        response["id"] = request_id
    return response


def mcp_error_response(request_id: str | int | None, code: int, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "jsonrpc": MCP_JSONRPC_VERSION,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if request_id is not None:
        response["id"] = request_id
    if data:
        response["error"]["data"] = data
    return response


def mcp_result_from_json_rpc_response(response: dict[str, Any]) -> dict[str, Any]:
    if isinstance(response.get("error"), dict):
        error = response["error"]
        safe_error = safe_tool_error(error.get("message") or "mcp_error")
        return {
            "content": [{"type": "text", "text": safe_error}],
            "structuredContent": {
                "tool_name": "unknown",
                "status": "error",
                "response": _safe_error_response(error.get("data") if isinstance(error.get("data"), dict) else {}),
                "error": safe_error,
                "idempotency_key": None,
            },
            "isError": True,
        }
    result = response.get("result")
    return result if isinstance(result, dict) else {}


def mcp_result_from_tool_result(result: ToolCallResult) -> dict[str, Any]:
    is_error = result.status == "error"
    response = _safe_error_response(result.response) if is_error else result.response
    error = safe_tool_error(result.error) if is_error else result.error
    structured_content = {
        "tool_name": result.tool_name,
        "status": result.status,
        "response": response,
        "error": error,
        "idempotency_key": result.idempotency_key,
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured_content, ensure_ascii=False),
            }
        ],
        "structuredContent": structured_content,
        "isError": is_error,
    }


def tool_result_from_mcp_result(tool_name: str, mcp_result: dict[str, Any], *, idempotency_key: str | None = None) -> ToolCallResult:
    structured = mcp_result.get("structuredContent") if isinstance(mcp_result.get("structuredContent"), dict) else {}
    status = "error" if mcp_result.get("isError") is True else str(structured.get("status") or "success")
    response = structured.get("response") if isinstance(structured.get("response"), dict) else structured
    error = safe_tool_error(structured.get("error")) if status == "error" else str(structured.get("error") or "")
    if not error and status == "error":
        error = safe_tool_error(_first_text_content(mcp_result))
    return ToolCallResult(
        tool_name=str(structured.get("tool_name") or tool_name),
        status=(
            "error"
            if status == "error"
            else "skipped"
            if status == "skipped"
            else "confirmation_required"
            if status == "confirmation_required"
            else "success"
        ),
        response=_safe_error_response(response) if status == "error" else response,
        error=error,
        idempotency_key=idempotency_key or structured.get("idempotency_key"),
    )


def mcp_tools_list(
    tools: list[dict[str, Any]],
    *,
    source_event_type: str | None = None,
    allowed_tool_names: list[str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"tools": tools}
    if source_event_type is not None:
        payload["source_event_type"] = source_event_type
    if allowed_tool_names is not None:
        payload["allowed_tool_names"] = allowed_tool_names
    return payload


def _first_text_content(mcp_result: dict[str, Any]) -> str:
    content = mcp_result.get("content")
    if not isinstance(content, list):
        return ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            return str(item.get("text") or "")
    return ""


def _safe_error_response(response: dict[str, Any]) -> dict[str, Any]:
    return redact_for_logging(response) if isinstance(response, dict) else {}


def safe_tool_error(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 80 and all(char.isascii() and (char.isalnum() or char in "_:-.") for char in text):
        return text
    return redacted_clinical_text_label(text, key="error")
