from __future__ import annotations

import re
from typing import Annotated, Final, Literal
from uuid import uuid4

from pydantic import StringConstraints
from starlette.requests import Request

PUBLIC_ID_HEX_LENGTH: Final = 16

PublicIdKind = Literal[
    "request",
    "patient",
    "user_message",
    "assistant_message",
    "meal",
    "food",
    "dose_event",
    "side_effect",
    "notification_policy",
]

PUBLIC_ID_PREFIXES: Final[dict[PublicIdKind, str]] = {
    "request": "req",
    "patient": "patient",
    "user_message": "user_msg",
    "assistant_message": "assistant_msg",
    "meal": "meal",
    "food": "food",
    "dose_event": "dose",
    "side_effect": "sidefx",
    "notification_policy": "npol",
}


def public_id_pattern(kind: PublicIdKind) -> str:
    prefix = PUBLIC_ID_PREFIXES[kind]
    return rf"^{re.escape(prefix)}_[0-9a-f]{{{PUBLIC_ID_HEX_LENGTH}}}$"


PUBLIC_ID_PATTERNS: Final[dict[PublicIdKind, re.Pattern[str]]] = {
    kind: re.compile(public_id_pattern(kind))
    for kind in PUBLIC_ID_PREFIXES
}

RequestId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("request"),
    ),
]
PatientId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("patient"),
    ),
]
UserMessageId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("user_message"),
    ),
]
AssistantMessageId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("assistant_message"),
    ),
]
MealId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("meal"),
    ),
]
FoodId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("food"),
    ),
]
DoseEventId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("dose_event"),
    ),
]
SideEffectId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("side_effect"),
    ),
]
NotificationPolicyId = Annotated[
    str,
    StringConstraints(
        strict=True,
        pattern=public_id_pattern("notification_policy"),
    ),
]


def new_public_id(kind: PublicIdKind) -> str:
    """Create a public contract identifier.

    Database writers remain responsible for retrying the operation if the
    corresponding UNIQUE constraint reports the extremely unlikely collision.
    """

    return f"{PUBLIC_ID_PREFIXES[kind]}_{uuid4().hex[:PUBLIC_ID_HEX_LENGTH]}"


def is_public_id(value: str, kind: PublicIdKind) -> bool:
    return PUBLIC_ID_PATTERNS[kind].fullmatch(value) is not None


def request_id_from_body(body: object) -> str | None:
    if not isinstance(body, dict):
        return None
    value = body.get("request_id")
    if not isinstance(value, str):
        return None
    return value if is_public_id(value, "request") else None


async def request_id_from_request(request: Request) -> str | None:
    try:
        body = await request.json()
    except (ValueError, RuntimeError):
        return None
    return request_id_from_body(body)


def require_public_id(value: str, kind: PublicIdKind) -> str:
    if not is_public_id(value, kind):
        raise ValueError(f"invalid_{kind}_id")
    return value
