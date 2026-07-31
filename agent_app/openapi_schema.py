from __future__ import annotations

from typing import Any

from shared.openapi_schema import SCHEMA_REF_PREFIX


def keep_only_ndjson_chat_success(
    paths: dict[str, Any],
    *,
    sync_chat_path: str,
) -> None:
    operation = paths.get(sync_chat_path, {}).get("post", {})
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return
    success = responses.get("200")
    if not isinstance(success, dict):
        return
    content = success.get("content")
    if not isinstance(content, dict):
        return
    ndjson = content.get("application/x-ndjson")
    success["content"] = (
        {"application/x-ndjson": ndjson}
        if isinstance(ndjson, dict)
        else {}
    )


def apply_conditional_contract_schemas(
    schemas: dict[str, Any],
) -> None:
    """Publish runtime cross-field rules that plain Pydantic JSON omits."""

    feedback = schemas.get("ChatFeedbackRequest")
    if isinstance(feedback, dict):
        feedback["anyOf"] = [
            {
                "required": ["reaction"],
                "properties": {
                    "reaction": {
                        "type": "string",
                        "enum": ["like", "dislike"],
                    }
                },
            },
            {
                "required": ["feedback_text"],
                "properties": {
                    "feedback_text": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 4000,
                    }
                },
            },
        ]

    chat_input = schemas.get("ChatInput")
    if isinstance(chat_input, dict):
        chat_input["oneOf"] = [
            {
                "required": ["type", "options"],
                "properties": {
                    "type": {"const": "number"},
                    "options": {
                        "allOf": [
                            {
                                "$ref": (
                                    f"{SCHEMA_REF_PREFIX}"
                                    "ChatInputOptions"
                                )
                            },
                            {
                                "type": "object",
                                "required": [
                                    "unit",
                                    "lower",
                                    "upper",
                                ],
                                "properties": {
                                    "unit": {"type": "string"},
                                    "lower": {"type": "number"},
                                    "upper": {"type": "number"},
                                    "selections": {"type": "null"},
                                },
                            },
                        ]
                    },
                },
            },
            {
                "required": ["type", "options"],
                "properties": {
                    "type": {"const": "dropdown"},
                    "options": {
                        "allOf": [
                            {
                                "$ref": (
                                    f"{SCHEMA_REF_PREFIX}"
                                    "ChatInputOptions"
                                )
                            },
                            {
                                "type": "object",
                                "required": ["selections"],
                                "properties": {
                                    "unit": {"type": "null"},
                                    "lower": {"type": "null"},
                                    "upper": {"type": "null"},
                                    "selections": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {
                                            "type": "string",
                                            "minLength": 1,
                                        },
                                    },
                                },
                            },
                        ]
                    },
                },
            },
        ]

    stream_event = schemas.get("ChatStreamEvent")
    if isinstance(stream_event, dict):
        stream_event["oneOf"] = [
            _streaming_event_schema(),
            _completed_event_schema("text"),
            _completed_event_schema("selection_box"),
            _completed_event_schema("input_box"),
            _error_event_schema(),
        ]


def _streaming_event_schema() -> dict[str, Any]:
    return {
        "properties": {
            "status": {"const": "streaming"},
            "message_type": {"const": "text"},
            "delta": {"type": "string", "minLength": 1},
            "message": {"type": "null"},
            "error": {"type": "null"},
        }
    }


def _completed_event_schema(
    message_type: str,
) -> dict[str, Any]:
    message_overlay: dict[str, Any] = {
        "type": "object",
        "properties": {
            "selections": {"type": "null"},
            "inputs": {"type": "null"},
        },
    }
    if message_type == "text":
        message_overlay["anyOf"] = [
            {
                "properties": {
                    "text": {"type": "string", "minLength": 1}
                }
            },
            {
                "properties": {
                    "message_title": {
                        "type": "string",
                        "minLength": 1,
                    }
                }
            },
            {
                "properties": {
                    "tables": {
                        "type": "array",
                        "minItems": 1,
                    }
                }
            },
        ]
    elif message_type == "selection_box":
        message_overlay["properties"] = {
            "selections": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "inputs": {"type": "null"},
        }
    else:
        message_overlay["properties"] = {
            "selections": {"type": "null"},
            "inputs": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "$ref": f"{SCHEMA_REF_PREFIX}ChatInput"
                },
            },
        }
    return {
        "properties": {
            "status": {"const": "completed"},
            "message_type": {"const": message_type},
            "delta": {"type": "null"},
            "message": {
                "allOf": [
                    {
                        "$ref": (
                            f"{SCHEMA_REF_PREFIX}"
                            "ChatMessageContent"
                        )
                    },
                    message_overlay,
                ]
            },
            "error": {"type": "null"},
        }
    }


def _error_event_schema() -> dict[str, Any]:
    return {
        "properties": {
            "status": {"const": "error"},
            "message_type": {"type": "null"},
            "delta": {"type": "null"},
            "message": {"type": "null"},
            "error": {
                "$ref": f"{SCHEMA_REF_PREFIX}ChatContractError"
            },
        }
    }
