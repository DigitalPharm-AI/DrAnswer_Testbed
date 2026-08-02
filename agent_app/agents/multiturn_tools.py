from __future__ import annotations

from typing import Any

from agent_app.agents.multiturn_state import MultiturnGraphState
from shared.schemas import AgentResponse, ToolCallResult


def append_blocked_supervisor_calls(
    blocked_calls: list[dict[str, Any]],
    executed_calls: list[dict[str, Any]],
    results: list[ToolCallResult],
    *,
    trace_id: str,
    confirmation_tool: str,
) -> None:
    for index, blocked_call in enumerate(blocked_calls):
        tool_name = str(blocked_call.get("name") or "unknown")
        executed_calls.append(blocked_call)
        results.append(
            ToolCallResult(
                tool_name=tool_name,
                status="skipped",
                response={
                    "reason": "blocked_by_pending_confirmation",
                    "confirmation_tool": confirmation_tool,
                },
                error="blocked_by_pending_confirmation",
                idempotency_key=(
                    f"{trace_id}:{tool_name}:supervisor-blocked:{index}"
                ),
            )
        )


def delegation_request_payload(
    state: MultiturnGraphState,
    delegated_call: dict[str, Any],
) -> dict[str, Any]:
    request_payload = state["request_payload"]
    if state.get("entry_mode") != "mutation_resolution":
        return request_payload

    raw_arguments = delegated_call.get("arguments")
    arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
    task = str(arguments.get("task") or "").strip()
    if not task:
        return request_payload

    scoped_payload = dict(request_payload)
    scoped_context = dict(request_payload.get("context") or {})
    scoped_context["supervisor_delegation"] = {
        "task": task,
        "reason": str(arguments.get("reason") or "").strip(),
        "original_user_message": request_payload.get("message"),
        "mutation_resolution": scoped_context.get("mutation_resolution"),
    }
    scoped_payload["message"] = task
    scoped_payload["context"] = scoped_context
    return scoped_payload


def delegation_tool_result(
    trace_id: str,
    delegated_call: dict[str, Any],
    delegated_response: AgentResponse,
    *,
    sequence: int,
) -> ToolCallResult:
    tool_name = str(delegated_call.get("name") or "delegated_agent")
    confirmation_required = (
        delegated_response.structured_payload.get("mutation_confirmation_required")
        is True
    )
    return ToolCallResult(
        tool_name=tool_name,
        status="confirmation_required" if confirmation_required else "success",
        response={
            "specialist_agent": delegated_response.agent_name,
            "decision_type": delegated_response.decision_type,
            "human_summary": delegated_response.human_summary,
            "structured_payload": delegated_response.structured_payload,
        },
        idempotency_key=f"{trace_id}:{tool_name}:delegation:{sequence}",
    )
