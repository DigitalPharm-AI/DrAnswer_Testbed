from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from time import perf_counter
from typing import Any

from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse
from shared.time_utils import utc_now

ModelCallObservation = dict[str, Any]

_MODEL_CALL_COLLECTOR: ContextVar[
    list[ModelCallObservation] | None
] = ContextVar("agent_model_call_collector", default=None)


@contextmanager
def capture_model_calls(
    observations: list[ModelCallObservation] | None = None,
) -> Iterator[list[ModelCallObservation]]:
    """Collect nested model calls for one logical Agent run.

    Only hashes, counters, timings, and model configuration are collected.
    Prompt and response content are never retained in the observation.
    """

    collector = observations if observations is not None else []
    token = _MODEL_CALL_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _MODEL_CALL_COLLECTOR.reset(token)


async def traced_model_ainvoke(
    model: Any,
    messages: list[Any],
    *,
    name: str,
    prompt_version_id: str,
) -> Any:
    started_at = utc_now()
    started = perf_counter()
    try:
        result = await model.ainvoke(messages)
    except Exception as exc:
        _record_observation(
            _model_observation(
                model=model,
                messages=messages,
                result=None,
                name=name,
                prompt_version_id=prompt_version_id,
                started_at=started_at,
                completed_at=utc_now(),
                latency_ms=_elapsed_ms(started),
                status="ERROR",
                error_code=type(exc).__name__,
                status_message=safe_exception_summary(exc, limit=300),
            )
        )
        raise
    _record_observation(
        _model_observation(
            model=model,
            messages=messages,
            result=result,
            name=name,
            prompt_version_id=prompt_version_id,
            started_at=started_at,
            completed_at=utc_now(),
            latency_ms=_elapsed_ms(started),
            status="COMPLETED",
        )
    )
    return result


async def traced_model_astream(
    model: Any,
    messages: list[Any],
    *,
    name: str,
    prompt_version_id: str,
) -> AsyncIterator[Any]:
    started_at = utc_now()
    started = perf_counter()
    first_content_at: datetime | None = None
    chunks: list[Any] = []
    try:
        async for chunk in model.astream(messages):
            if first_content_at is None and _has_public_content(chunk):
                first_content_at = utc_now()
            chunks.append(chunk)
            yield chunk
    except Exception as exc:
        completed_at = utc_now()
        _record_observation(
            _model_observation(
                model=model,
                messages=messages,
                result=chunks,
                name=name,
                prompt_version_id=prompt_version_id,
                started_at=started_at,
                completed_at=completed_at,
                completion_start_time=first_content_at,
                latency_ms=_elapsed_ms(started),
                status="ERROR",
                error_code=type(exc).__name__,
                status_message=safe_exception_summary(exc, limit=300),
            )
        )
        raise
    completed_at = utc_now()
    _record_observation(
        _model_observation(
            model=model,
            messages=messages,
            result=chunks,
            name=name,
            prompt_version_id=prompt_version_id,
            started_at=started_at,
            completed_at=completed_at,
            completion_start_time=first_content_at,
            latency_ms=_elapsed_ms(started),
            status="COMPLETED",
        )
    )


def response_with_model_calls(
    response: AgentResponse,
    observations: list[ModelCallObservation],
) -> AgentResponse:
    if not observations:
        return response
    structured = dict(response.structured_payload)
    structured["model_call_observations"] = [
        {
            key: value
            for key, value in observation.items()
            if key != "decision_evidence"
        }
        for observation in observations
    ]
    usage = aggregate_token_usage(observations)
    if usage["input_tokens"] or usage["output_tokens"]:
        structured["token_usage"] = usage
    return response.model_copy(
        update={"structured_payload": structured}
    )


def aggregate_token_usage(
    observations: list[ModelCallObservation],
) -> dict[str, int]:
    input_tokens = 0
    output_tokens = 0
    for observation in observations:
        usage = observation.get("usage_details")
        if not isinstance(usage, dict):
            continue
        input_tokens += _nonnegative_int(
            usage.get("input")
            or usage.get("input_tokens")
        )
        output_tokens += _nonnegative_int(
            usage.get("output")
            or usage.get("output_tokens")
        )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _record_observation(observation: ModelCallObservation) -> None:
    collector = _MODEL_CALL_COLLECTOR.get()
    if collector is not None:
        collector.append(observation)


def _model_observation(
    *,
    model: Any,
    messages: list[Any],
    result: Any,
    name: str,
    prompt_version_id: str,
    started_at: datetime,
    completed_at: datetime,
    latency_ms: int,
    status: str,
    completion_start_time: datetime | None = None,
    error_code: str = "",
    status_message: str = "",
) -> ModelCallObservation:
    usage_details = _usage_details(result)
    identity = _model_identity(model)
    return {
        "observation_id": uuid.uuid4().hex,
        "observation_type": "generation",
        "name": name,
        "status": status,
        "level": "ERROR" if status == "ERROR" else "DEFAULT",
        "status_message": status_message,
        "error_code": error_code,
        "prompt_version_id": prompt_version_id,
        "provider": identity["provider"],
        "model_id": identity["model_id"],
        "model_parameters": identity["model_parameters"],
        "input_hash": _sha256_json(_message_projection(messages)),
        "output_hash": _sha256_json(_result_projection(result)),
        "decision_evidence": _decision_evidence(
            messages,
            result,
        ),
        "usage_details": usage_details,
        "started_at": started_at.isoformat(),
        "completion_start_time": (
            completion_start_time.isoformat()
            if completion_start_time is not None
            else None
        ),
        "completed_at": completed_at.isoformat(),
        "latency_ms": latency_ms,
        "time_to_first_token_ms": (
            max(
                0,
                round(
                    (
                        completion_start_time - started_at
                    ).total_seconds()
                    * 1000
                ),
            )
            if completion_start_time is not None
            else 0
        ),
    }


def _decision_evidence(
    messages: list[Any],
    result: Any,
) -> dict[str, Any]:
    values = result if isinstance(result, list) else [result]
    public_chunks: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_fragments: list[str] = []
    reasoning_block_count = 0
    for value in values:
        content = getattr(value, "content", None)
        if isinstance(content, str):
            if content:
                public_chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    public_chunks.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                if item_type == "text" and item.get("text") is not None:
                    public_chunks.append(str(item["text"]))
                elif item_type in {
                    "reasoning_content",
                    "thinking",
                }:
                    reasoning_block_count += 1
                    reasoning_value = (
                        item.get("reasoning_content")
                        if item_type == "reasoning_content"
                        else item.get("thinking")
                    )
                    if isinstance(reasoning_value, dict):
                        reasoning_text = str(
                            reasoning_value.get("text") or ""
                        )
                    else:
                        reasoning_text = str(
                            reasoning_value or ""
                        )
                    if reasoning_text:
                        reasoning_fragments.append(reasoning_text)
        for call in getattr(value, "tool_calls", None) or []:
            if not isinstance(call, dict):
                continue
            arguments = call.get("args")
            if not isinstance(arguments, dict):
                arguments = call.get("arguments")
            tool_calls.append(
                {
                    "tool_name": str(call.get("name") or ""),
                    "arguments": (
                        arguments
                        if isinstance(arguments, dict)
                        else {}
                    ),
                }
            )

    input_context = _decision_input_context(messages)
    tool_names = [
        str(call.get("tool_name") or "")
        for call in tool_calls
    ]
    reason_code = _system_decision_reason_code(
        input_context,
        tool_names,
        bool(public_chunks),
    )
    private_reasoning_text = "".join(reasoning_fragments)
    return {
        "evidence_version": "v1",
        "reason_code_source": "system_projection",
        "system_reason_code": reason_code,
        "input_context": input_context,
        "public_output": "".join(public_chunks),
        "tool_calls": tool_calls,
        "private_reasoning": {
            "retained": False,
            "present": bool(
                reasoning_block_count
                or private_reasoning_text
            ),
            "block_count": reasoning_block_count,
            "character_count": len(private_reasoning_text),
            "sha256": (
                hashlib.sha256(
                    private_reasoning_text.encode("utf-8")
                ).hexdigest()
                if private_reasoning_text
                else ""
            ),
        },
    }


def _decision_input_context(
    messages: list[Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for message in messages:
        if type(message).__name__ != "HumanMessage":
            continue
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            continue
        try:
            candidate = json.loads(content)
        except json.JSONDecodeError:
            candidate = {}
        if isinstance(candidate, dict):
            payload = candidate
            break
    context = (
        payload.get("context")
        if isinstance(payload.get("context"), dict)
        else {}
    )
    structured = (
        context.get("structured_response_context")
        if isinstance(
            context.get("structured_response_context"),
            dict,
        )
        else {}
    )
    source_message = (
        structured.get("source_message")
        if isinstance(structured.get("source_message"), dict)
        else {}
    )
    source_payload = (
        source_message.get("message")
        if isinstance(source_message.get("message"), dict)
        else {}
    )
    return {
        "message": str(payload.get("message") or ""),
        "structured_response_type": str(
            structured.get("response_type") or ""
        ),
        "selected_value": str(
            structured.get("response_value") or ""
        ),
        "source_message_type": str(
            source_message.get("message_type") or ""
        ),
        "source_selection_count": len(
            source_payload.get("selections")
        )
        if isinstance(source_payload.get("selections"), list)
        else 0,
        "source_has_food_ref_id": _contains_key(
            source_payload,
            "food_ref_id",
        ),
        "source_has_nutrients": _contains_key(
            source_payload,
            "nutrients",
        ),
    }


def _system_decision_reason_code(
    input_context: dict[str, Any],
    tool_names: list[str],
    has_public_output: bool,
) -> str:
    if (
        "search_nutrition_food_candidates" in tool_names
        and input_context.get("selected_value")
        and not input_context.get("source_has_food_ref_id")
    ):
        return (
            "STRUCTURED_SELECTION_AUTHORITATIVE_FOOD_LOOKUP"
        )
    if tool_names:
        return "MODEL_SELECTED_TOOL"
    if has_public_output:
        return "MODEL_FINAL_RESPONSE"
    return "MODEL_EMPTY_RESPONSE"


def _contains_key(value: Any, target: str) -> bool:
    if isinstance(value, dict):
        return target in value or any(
            _contains_key(child, target)
            for child in value.values()
        )
    if isinstance(value, list):
        return any(
            _contains_key(child, target)
            for child in value
        )
    return False


def _model_identity(model: Any) -> dict[str, Any]:
    current = model
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        model_id = _first_nonempty_attribute(
            current,
            "model_id",
            "model",
            "model_name",
        )
        parameters = _safe_model_parameters(current)
        if model_id or parameters:
            module = type(current).__module__.lower()
            if "bedrock" in module:
                provider = "bedrock"
            elif "anthropic" in module:
                provider = "anthropic"
            elif "openai" in module:
                provider = "openai"
            else:
                provider = type(current).__name__
            return {
                "provider": provider,
                "model_id": model_id,
                "model_parameters": parameters,
            }
        current = getattr(current, "bound", None)
    return {
        "provider": type(model).__name__,
        "model_id": "",
        "model_parameters": {},
    }


def _safe_model_parameters(model: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for key in (
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
    ):
        value = getattr(model, key, None)
        if isinstance(value, (str, int, float, bool)) or value is None:
            if value is not None:
                parameters[key] = value
    return parameters


def _first_nonempty_attribute(model: Any, *names: str) -> str:
    for name in names:
        value = getattr(model, name, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _usage_details(result: Any) -> dict[str, int]:
    candidates: list[dict[str, Any]] = []
    values = result if isinstance(result, list) else [result]
    for value in values:
        usage = getattr(value, "usage_metadata", None)
        if isinstance(usage, dict):
            candidates.append(usage)
        response_metadata = getattr(value, "response_metadata", None)
        if not isinstance(response_metadata, dict):
            continue
        candidates.append(response_metadata)
        for key in (
            "usage",
            "usage_metadata",
            "token_usage",
        ):
            nested = response_metadata.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_creation_tokens = 0
    reasoning_tokens = 0
    for usage in candidates:
        input_tokens = max(
            input_tokens,
            _first_count(
                usage,
                "input_tokens",
                "inputTokens",
                "inputTokenCount",
                "prompt_tokens",
            ),
        )
        output_tokens = max(
            output_tokens,
            _first_count(
                usage,
                "output_tokens",
                "outputTokens",
                "outputTokenCount",
                "completion_tokens",
            ),
        )
        cache_read_tokens = max(
            cache_read_tokens,
            _first_count(
                usage,
                "input_token_details.cache_read",
                "cache_read_input_tokens",
                "cache_read_tokens",
            ),
        )
        cache_creation_tokens = max(
            cache_creation_tokens,
            _first_count(
                usage,
                "cache_creation_input_tokens",
                "cache_write_tokens",
            ),
        )
        reasoning_tokens = max(
            reasoning_tokens,
            _first_count(
                usage,
                "output_token_details.reasoning",
                "reasoning_tokens",
            ),
        )
    details = {
        "input": input_tokens,
        "output": output_tokens,
    }
    if cache_read_tokens:
        details["cache_read_input_tokens"] = cache_read_tokens
    if cache_creation_tokens:
        details["cache_creation_input_tokens"] = cache_creation_tokens
    if reasoning_tokens:
        details["reasoning_tokens"] = reasoning_tokens
    return details


def _first_count(mapping: dict[str, Any], *paths: str) -> int:
    for path in paths:
        value: Any = mapping
        for part in path.split("."):
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        parsed = _nonnegative_int(value)
        if parsed:
            return parsed
    return 0


def _message_projection(messages: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": type(message).__name__,
            "content_hash": _sha256_json(
                getattr(message, "content", "")
            ),
            "tool_call_count": len(
                getattr(message, "tool_calls", None) or []
            ),
        }
        for message in messages
    ]


def _result_projection(result: Any) -> Any:
    if isinstance(result, list):
        return [_single_result_projection(value) for value in result]
    return _single_result_projection(result)


def _single_result_projection(result: Any) -> dict[str, Any]:
    return {
        "type": type(result).__name__ if result is not None else "None",
        "content_hash": _sha256_json(
            getattr(result, "content", "")
        ),
        "tool_call_count": len(
            getattr(result, "tool_calls", None) or []
        ),
    }


def _has_public_content(chunk: Any) -> bool:
    content = getattr(chunk, "content", None)
    if isinstance(content, str):
        return bool(content)
    return isinstance(content, list) and bool(content)


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
