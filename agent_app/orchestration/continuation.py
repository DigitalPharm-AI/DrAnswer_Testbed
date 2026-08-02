from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from shared.schemas import AgentResponse
from shared.tool_names import POLICY_TOOLS, SIDE_EFFECT_TOOLS

DEFAULT_MAX_CONTINUATION_STEPS = 1


class ContinuationLimitExceeded(RuntimeError):
    """Raised when an agent keeps returning provisional continuation responses."""


def continuation_type(tool_calls: list[dict[str, Any]]) -> str:
    names = {str(call.get("name") or "") for call in tool_calls}
    if names.intersection(SIDE_EFFECT_TOOLS):
        return "side_effect_assessment"
    if names.intersection(POLICY_TOOLS):
        return "policy_change_request"
    return ""


def requires_continuation(response: AgentResponse) -> bool:
    return response.structured_payload.get("continuation_required") is True


def continuation_payload(
    payload: dict[str, Any],
    response: AgentResponse,
) -> dict[str, Any]:
    continuation = dict(payload)
    context = dict(continuation.get("context") or {})
    confirmation_reply = response.structured_payload.get("mutation_confirmation_reply")
    if isinstance(confirmation_reply, dict) and confirmation_reply.get("intent") in {
        "new_request",
        "revise",
    }:
        intent = str(confirmation_reply.get("intent"))
        context.pop("pending_mutation_confirmation", None)
        context["pending_mutation_confirmation_reply_resolved"] = intent
        revision = response.structured_payload.get("mutation_confirmation_revision")
        if intent == "revise" and isinstance(revision, dict):
            context["mutation_confirmation_revision"] = revision
    context["execute_tool_continuation"] = True
    context["continuation_type"] = response.structured_payload.get(
        "continuation_type",
        "",
    )
    context["continuation_tool_calls"] = response.structured_payload.get(
        "tool_calls",
        [],
    )
    continuation["context"] = context
    return continuation


async def resolve_required_continuations(
    payload: dict[str, Any],
    response: AgentResponse,
    invoke: Callable[[dict[str, Any]], Awaitable[AgentResponse]],
    *,
    max_steps: int = DEFAULT_MAX_CONTINUATION_STEPS,
) -> AgentResponse:
    """Resolve deferred work without ever returning a provisional response.

    The caller owns the overall timeout. Keeping the bound here makes the
    synchronous chat API fail closed if an orchestrator repeatedly asks for
    another continuation.
    """

    if max_steps < 0:
        raise ValueError("max_steps_must_not_be_negative")

    current_payload = payload
    current_response = response
    steps = 0
    while requires_continuation(current_response):
        if steps >= max_steps:
            raise ContinuationLimitExceeded(
                f"continuation_limit_exceeded:{max_steps}"
            )
        current_payload = continuation_payload(current_payload, current_response)
        current_response = await invoke(current_payload)
        steps += 1
    return current_response
