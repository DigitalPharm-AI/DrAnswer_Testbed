from __future__ import annotations

from functools import cache
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError, best_match

from shared.tool_catalog import ToolCatalog


@cache
def _validator_for(tool_name: str) -> Draft202012Validator:
    tools = ToolCatalog.tools_for(tool_name)
    if not tools:
        raise KeyError(f"unknown_tool:{tool_name}")
    schema = tools[0].get("inputSchema")
    if not isinstance(schema, dict):
        raise ValueError(f"tool_input_schema_missing:{tool_name}")
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(
        schema,
        format_checker=FormatChecker(),
    )


def tool_argument_validation_error(
    tool_name: str,
    arguments: dict[str, Any],
) -> str | None:
    """Validate Tool arguments against the canonical ToolCatalog schema."""

    try:
        validator = _validator_for(tool_name)
    except (KeyError, ValueError) as exc:
        return str(exc)
    error = best_match(validator.iter_errors(arguments))
    if error is None:
        return None
    return _validation_error_text(error)


def _validation_error_text(error: ValidationError) -> str:
    path = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}"
    return f"{path}: {error.message}"
