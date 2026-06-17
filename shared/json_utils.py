from __future__ import annotations

import json


def dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def parse_json_object(value: str | None) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except ValueError:
        return []
    return [item for item in parsed if isinstance(item, str)] if isinstance(parsed, list) else []
