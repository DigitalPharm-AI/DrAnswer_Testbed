from __future__ import annotations

from collections.abc import Iterable
from typing import Any

SCHEMA_REF_PREFIX = "#/components/schemas/"


def referenced_schemas(
    root: Any,
    available_schemas: dict[str, Any],
) -> dict[str, Any]:
    """Return the transitive component-schema closure used by ``root``."""

    pending = list(_schema_references(root))
    selected: dict[str, Any] = {}
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        schema = available_schemas.get(name)
        if schema is None:
            raise KeyError(f"missing_openapi_schema:{name}")
        selected[name] = schema
        pending.extend(_schema_references(schema))
    return {name: selected[name] for name in sorted(selected)}


def _schema_references(value: Any) -> Iterable[str]:
    if isinstance(value, dict):
        reference = value.get("$ref")
        if (
            isinstance(reference, str)
            and reference.startswith(SCHEMA_REF_PREFIX)
        ):
            yield reference.removeprefix(SCHEMA_REF_PREFIX)
        for item in value.values():
            yield from _schema_references(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_references(item)
