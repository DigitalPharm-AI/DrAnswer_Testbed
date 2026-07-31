from __future__ import annotations

from typing import Any


def is_retired_conversation_key(value: object) -> bool:
    """Return whether a mapping key represents the retired conversation ID.

    The v1.3 contract has one patient-scoped thread, so this identifier must
    not cross Frontend/Backend/AI boundaries even when it is hidden inside a
    generic metadata or Tool argument object.
    """

    normalized = "".join(
        character
        for character in str(value).casefold()
        if character.isalnum()
    )
    return normalized == "conversationid"


def remove_retired_conversation_fields(value: Any) -> Any:
    """Recursively remove retired conversation identifiers from JSON values."""

    if isinstance(value, dict):
        return {
            key: remove_retired_conversation_fields(item)
            for key, item in value.items()
            if not is_retired_conversation_key(key)
        }
    if isinstance(value, list):
        return [
            remove_retired_conversation_fields(item)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            remove_retired_conversation_fields(item)
            for item in value
        )
    return value
