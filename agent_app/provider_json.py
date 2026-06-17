from __future__ import annotations

import json
import re
from typing import Any


def strip_json_fence(content: str) -> str:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1)
    stripped = content.strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if 0 <= start < end:
        return stripped[start : end + 1]
    return stripped


def parse_json_object(content: str) -> dict[str, Any]:
    payload = json.loads(strip_json_fence(content))
    if not isinstance(payload, dict):
        raise ValueError("LLM response must be a JSON object.")
    return payload
