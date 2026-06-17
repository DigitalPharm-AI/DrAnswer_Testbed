from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_tool_calls(output: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    direct = output.get("tool_call")
    if isinstance(direct, dict):
        calls.append(direct)
    raw_many = output.get("tool_calls")
    if isinstance(raw_many, list):
        calls.extend(item for item in raw_many if isinstance(item, dict))
    return order_tool_calls(calls)


def order_tool_calls(calls: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed = list(enumerate(calls))
    priority = {"lookup_side_effect_info": 10, "AE_pro_ctcae": 20}
    return [call for _, call in sorted(indexed, key=lambda item: (priority.get(str(item[1].get("name")), 0), item[0]))]
