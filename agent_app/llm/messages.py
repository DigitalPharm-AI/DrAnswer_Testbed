from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent_app.providers.parsing import parse_json_object
from agent_app.llm.responses import natural_chat_summary
from shared.schemas import ToolCallResult


def build_chat_messages(system_prompt: str, payload: dict[str, Any]) -> list[BaseMessage]:
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    ]


def langchain_tools_from_catalog(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the internal MCP-ish catalog shape to LangChain tool specs."""

    converted: list[dict[str, Any]] = []
    for tool in tools:
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or tool.get("title") or name),
                    "parameters": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def system_prompt_from_messages(messages: list[BaseMessage]) -> str:
    for message in messages:
        if isinstance(message, SystemMessage):
            return _content_text(message.content)
    return ""


def human_payload_from_messages(messages: list[BaseMessage]) -> dict[str, Any]:
    for message in messages:
        if isinstance(message, HumanMessage):
            text = _content_text(message.content)
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                return {"message": text}
            return payload if isinstance(payload, dict) else {"message": text}
    return {}


def ai_message_from_tool_calls(tool_calls: list[dict[str, Any]], *, content: str = "", model_output: dict[str, Any] | None = None) -> AIMessage:
    return AIMessage(
        content=content,
        tool_calls=[
            {
                "name": str(call.get("name") or ""),
                "args": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                "id": str(call.get("id") or _tool_call_id(index, call)),
            }
            for index, call in enumerate(tool_calls)
            if str(call.get("name") or "")
        ],
        response_metadata={"model_output": model_output or {"tool_calls": tool_calls}},
    )


def tool_calls_from_ai_message(message: AIMessage) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for call in message.tool_calls:
        name = str(call.get("name") or "")
        if not name:
            continue
        raw_args = call.get("args")
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = {}
            arguments = parsed if isinstance(parsed, dict) else {}
        else:
            arguments = {}
        payload = {"name": name, "arguments": arguments}
        if call.get("id"):
            payload["id"] = str(call["id"])
        calls.append(payload)
    return calls


def tool_messages_from_results(ai_message: AIMessage, executed_calls: list[dict[str, Any]], results: list[ToolCallResult]) -> list[ToolMessage]:
    ids = [str(call.get("id") or _tool_call_id(index, {})) for index, call in enumerate(ai_message.tool_calls)]
    messages: list[ToolMessage] = []
    for index, result in enumerate(results):
        tool_call_id = ids[index] if index < len(ids) else _tool_call_id(index, executed_calls[index] if index < len(executed_calls) else {})
        messages.append(
            ToolMessage(
                content=json.dumps(result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
                tool_call_id=tool_call_id,
                name=result.tool_name,
            )
        )
    return messages


def tool_results_from_messages(messages: list[BaseMessage]) -> list[ToolCallResult]:
    results: list[ToolCallResult] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        try:
            payload = json.loads(_content_text(message.content))
        except json.JSONDecodeError:
            payload = {"tool_name": getattr(message, "name", "") or "unknown", "status": "error", "error": _content_text(message.content)}
        if isinstance(payload, dict):
            results.append(ToolCallResult.model_validate(payload))
    return results


def model_output_from_ai_message(message: AIMessage) -> dict[str, Any]:
    metadata = message.response_metadata if isinstance(message.response_metadata, dict) else {}
    raw = metadata.get("model_output")
    if isinstance(raw, dict):
        return raw
    content = _content_text(message.content).strip()
    if not content:
        return {}
    try:
        parsed = parse_json_object(content)
    except Exception:
        return {"message": content}
    return parsed if isinstance(parsed, dict) else {"message": content}


def patient_summary_with_source(message: AIMessage, fallback: str, *, fallback_source: str = "fallback") -> tuple[str, str]:
    output = model_output_from_ai_message(message)
    if output:
        summary = natural_chat_summary(output)
        if summary:
            return summary, str(output.get("fallback") or "model_output")
    content = _content_text(message.content).strip()
    if content:
        return content, "message_content"
    if fallback:
        return fallback, fallback_source
    return "", "missing"


def patient_summary_from_ai_message(message: AIMessage, fallback: str) -> str:
    summary, _source = patient_summary_with_source(message, fallback)
    return summary


def langchain_tool_name(tool: dict[str, Any]) -> str:
    function = tool.get("function") if isinstance(tool.get("function"), dict) else {}
    return str(function.get("name") or tool.get("name") or "")


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and item.get("text") is not None:
                    chunks.append(str(item["text"]))
                elif item.get("text") is not None:
                    chunks.append(str(item["text"]))
        return "".join(chunks)
    return str(content or "")


def _tool_call_id(index: int, call: dict[str, Any]) -> str:
    name = str(call.get("name") or "tool")
    return f"call_{index}_{name}"
