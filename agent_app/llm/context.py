from __future__ import annotations

from typing import Any


def context_value(payload: dict[str, Any], key: str) -> Any:
    context = payload.get("context")
    if isinstance(context, dict):
        return context.get(key)
    return None
