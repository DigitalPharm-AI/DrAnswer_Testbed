from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from agent_app.providers.parsing import parse_json_object
from agent_app.llm.responses import natural_chat_summary
from shared.tool_names import (
    GET_MEDICATION_DOSE_STATUS,
    GET_NOTIFICATION_POLICIES,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
)
from shared.schemas import ToolCallResult

_LLM_HIDDEN_CONTEXT_KEYS = frozenset(
    {
        "backend_read_context",
        "approved_mutation_confirmation",
        "approved_user_action",
        "completed_pro_ctcae_survey",
        "request_metadata",
        "trusted_patient_context",
    }
)
_LLM_PROJECTED_CONTEXT_KEYS = frozenset(
    {
        "mutation_resolution",
        "patient_context_snapshot",
        "recent_chat",
        "structured_response_context",
    }
)
_LLM_HIDDEN_TECHNICAL_FIELDS = frozenset(
    {
        "id",
        "action_fingerprint",
        "approval_key",
        "confirmation_id",
        "confirmation_message_id",
        "dose_event_id",
        "expected_version",
        "food_id",
        "food_ref_id",
        "idempotency_key",
        "job_id",
        "meal_id",
        "message_id",
        "notification_id",
        "parent_record_id",
        "patient_id",
        "record_id",
        "request_id",
        "response_message_id",
        "source_chat_request_id",
        "source_message_id",
        "source_trace_id",
        "tool_call_id",
        "trace_id",
        "version",
        "write_request_id",
    }
)
_MODEL_PUBLIC_RESULT_IDS: dict[str, frozenset[str]] = {
    GET_MEDICATION_DOSE_STATUS: frozenset({"dose_event_id"}),
    GET_NUTRITION_MEAL_RECORD_LIST: frozenset(
        {"meal_id", "food_id", "food_ref_id"}
    ),
    SEARCH_NUTRITION_FOOD_CANDIDATES: frozenset({"food_ref_id"}),
    GET_NUTRITION_RECOMMENDATION_CANDIDATES: frozenset({"food_ref_id"}),
    GET_NOTIFICATION_POLICIES: frozenset({"policy_id"}),
}


class LlmOutputParseError(ValueError):
    """The model did not return the required structured JSON object."""


class LlmToolArgumentsParseError(ValueError):
    """A native model Tool call did not contain a valid JSON object."""


def build_chat_messages(system_prompt: str, payload: dict[str, Any]) -> list[BaseMessage]:
    return [
        SystemMessage(content=system_prompt),
        HumanMessage(
            content=json.dumps(
                _llm_safe_payload(payload),
                ensure_ascii=False,
                sort_keys=True,
                default=_json_default,
            )
        ),
    ]


def _llm_safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    safe.pop("patient_id", None)
    context = safe.get("context")
    if isinstance(context, dict):
        safe_context: dict[str, Any] = {}
        for key, value in context.items():
            if key in _LLM_HIDDEN_CONTEXT_KEYS:
                continue
            safe_context[key] = (
                _llm_visible_projection(value)
                if key in _LLM_PROJECTED_CONTEXT_KEYS
                else value
            )
        safe["context"] = safe_context
    projected = _llm_visible_projection(safe)
    return projected if isinstance(projected, dict) else {}


def _llm_visible_projection(
    value: Any,
    *,
    allowed_public_ids: frozenset[str] = frozenset(),
) -> Any:
    """Keep model-relevant meaning while removing backend/tool identifiers."""

    if isinstance(value, dict):
        return {
            key: _llm_visible_projection(
                item,
                allowed_public_ids=allowed_public_ids,
            )
            for key, item in value.items()
            if not _llm_hidden_technical_field(
                key,
                allowed_public_ids=allowed_public_ids,
            )
        }
    if isinstance(value, (list, tuple)):
        return [
            _llm_visible_projection(
                item,
                allowed_public_ids=allowed_public_ids,
            )
            for item in value
        ]
    return value


def _llm_hidden_technical_field(
    key: str,
    *,
    allowed_public_ids: frozenset[str] = frozenset(),
) -> bool:
    if key == "policy_id" or key in allowed_public_ids:
        return False
    return key in _LLM_HIDDEN_TECHNICAL_FIELDS or key.endswith("_id")


def _json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


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


def ai_message_from_tool_calls(
    tool_calls: list[dict[str, Any]],
    *,
    content: Any = "",
    model_output: dict[str, Any] | None = None,
) -> AIMessage:
    """Keep signed Bedrock reasoning blocks intact across a tool round."""

    native_tool_calls: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        name = str(call.get("name") or "")
        if not name:
            continue
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            raise LlmToolArgumentsParseError(
                "llm_tool_arguments_missing_or_invalid"
            )
        native_tool_calls.append(
            {
                "name": name,
                "args": arguments,
                "id": str(
                    call.get("id")
                    or _tool_call_id(index, call)
                ),
            }
        )
    return AIMessage(
        content=content,
        tool_calls=native_tool_calls,
        response_metadata={"model_output": model_output or {"tool_calls": tool_calls}},
    )


def tool_calls_from_ai_message(message: AIMessage) -> list[dict[str, Any]]:
    if message.invalid_tool_calls:
        raise LlmToolArgumentsParseError(
            "llm_tool_arguments_invalid"
        )
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
            except json.JSONDecodeError as exc:
                raise LlmToolArgumentsParseError(
                    "llm_tool_arguments_invalid_json"
                ) from exc
            if not isinstance(parsed, dict):
                raise LlmToolArgumentsParseError(
                    "llm_tool_arguments_must_be_object"
                )
            arguments = parsed
        else:
            raise LlmToolArgumentsParseError(
                "llm_tool_arguments_missing_or_invalid"
            )
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
                content=json.dumps(
                    _llm_visible_projection(
                        result.model_dump(mode="json"),
                        allowed_public_ids=_MODEL_PUBLIC_RESULT_IDS.get(
                            result.tool_name,
                            frozenset(),
                        ),
                    ),
                    ensure_ascii=False,
                    sort_keys=True,
                ),
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
        output = dict(raw)
    elif "model_output" in metadata and raw is not None:
        raise LlmOutputParseError(
            "llm_model_output_metadata_must_be_object"
        )
    else:
        content = _content_text(message.content).strip()
        if not content:
            output = {}
        else:
            try:
                output = parse_json_object(content)
            except (json.JSONDecodeError, ValueError) as exc:
                raise LlmOutputParseError(
                    "llm_output_invalid_json_object"
                ) from exc

    usage = _token_usage_from_ai_message(message)
    if usage:
        existing = (
            output.get("token_usage")
            if isinstance(output.get("token_usage"), dict)
            else {}
        )
        output["token_usage"] = {
            "input_tokens": _nonnegative_token_count(
                existing.get("input_tokens")
                or usage.get("input_tokens")
            ),
            "output_tokens": _nonnegative_token_count(
                existing.get("output_tokens")
                or usage.get("output_tokens")
            ),
        }
    return output


def public_text_from_ai_message(message: AIMessage) -> str:
    """Return only user-visible text blocks from an AI message.

    General agent-loop answers are plain text, not JSON model outputs. Keeping
    this extraction separate from ``model_output_from_ai_message`` prevents
    reasoning/signature blocks from becoming part of the public response.
    """

    content = message.content
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""

    chunks: list[str] = []
    for item in content:
        if isinstance(item, str):
            chunks.append(item)
        elif (
            isinstance(item, dict)
            and item.get("type") == "text"
            and item.get("text") is not None
        ):
            chunks.append(str(item["text"]))
    return "".join(chunks).strip()


def model_output_with_tool_calls(
    model_output: dict[str, Any],
    tool_calls: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize a provider response to the canonical multi-tool shape."""

    merged = dict(model_output)
    if tool_calls:
        merged.pop("tool_call", None)
        merged["tool_calls"] = tool_calls
    return merged


def _token_usage_from_ai_message(
    message: AIMessage,
) -> dict[str, int]:
    candidates: list[dict[str, Any]] = []
    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, dict):
        candidates.append(usage_metadata)
    response_metadata = (
        message.response_metadata
        if isinstance(message.response_metadata, dict)
        else {}
    )
    for key in ("usage", "usage_metadata"):
        value = response_metadata.get(key)
        if isinstance(value, dict):
            candidates.append(value)

    for usage in candidates:
        input_tokens = _first_token_count(
            usage,
            "input_tokens",
            "inputTokens",
            "prompt_tokens",
        )
        output_tokens = _first_token_count(
            usage,
            "output_tokens",
            "outputTokens",
            "completion_tokens",
        )
        if input_tokens or output_tokens:
            return {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
    return {}


def _first_token_count(
    usage: dict[str, Any],
    *keys: str,
) -> int:
    for key in keys:
        if key in usage:
            return _nonnegative_token_count(usage.get(key))
    return 0


def _nonnegative_token_count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
