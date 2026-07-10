from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent_app.tool_names import GET_MEDICATION_SIDE_EFFECT_ASSESSMENT, GET_PRO_CTCAE_QUESTIONNAIRE


def normalize_tool_calls(output: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    direct = output.get("tool_call")
    if isinstance(direct, dict):
        calls.append(_normalize_tool_call(direct))
    raw_many = output.get("tool_calls")
    if isinstance(raw_many, list):
        calls.extend(_normalize_tool_call(item) for item in raw_many if isinstance(item, dict))
    return order_tool_calls(calls)


def order_tool_calls(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(calls))
    priority = {GET_MEDICATION_SIDE_EFFECT_ASSESSMENT: 10, GET_PRO_CTCAE_QUESTIONNAIRE: 20}
    return [call for _, call in sorted(indexed, key=lambda item: (priority.get(str(item[1].get("name")), 0), item[0]))]


def _normalize_tool_call(call: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(call)
    if not normalized.get("name") and normalized.get("tool_name"):
        normalized["name"] = normalized["tool_name"]
    if not isinstance(normalized.get("arguments"), dict) and isinstance(normalized.get("args"), dict):
        normalized["arguments"] = normalized["args"]
    return normalized
