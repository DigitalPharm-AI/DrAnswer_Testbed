from __future__ import annotations

from typing import Any

from agent_app.agents.multiturn_output import (
    MULTITURN_CHAT_AGENT_NAME,
    required_llm_summary,
)
from agent_app.agents.multiturn_state import MultiturnGraphState
from agent_app.agents.tool_chat import (
    ITERATIVE_FINALIZATION_MODE,
    ITERATIVE_TOOL_EXECUTION_MODE,
    TOOL_LOOP_MODE,
)
from agent_app.llm.generation import PROMPT_VERSION_ID
from agent_app.orchestration.delegation import delegation_reason, is_delegation_tool_call
from agent_app.tools.policy import has_deferred_policy_tool_call
from agent_app.tools.results import tool_calls_payload
from shared.chat_contracts import has_structured_ui_response
from shared.schemas import AgentResponse
from shared.tool_names import SIDE_EFFECT_TOOLS


def confirmation_reply_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    output = state["confirmation_reply_model_output"]
    intent = str(output["intent"])
    human_summary = str(output["message"]).strip()
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="mutation_confirmation_reply",
            structured_payload={
                "routing_mode": "mutation_confirmation_reply",
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "mutation_confirmation_reply": {"intent": intent},
                "model_output": output,
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "final_answer_source": "confirmation_reply_llm",
                "message_flow": ["HumanMessage", "AIMessage(confirmation_reply)"],
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": "none",
                "finalization_mode": "confirmation_reply_interpretation",
                "node_timings_ms": {
                    "mutation_confirmation_reply_llm": state.get(
                        "confirmation_reply_llm_elapsed_ms",
                        0,
                    ),
                },
                "iterations": 0,
            },
            human_summary=human_summary,
            requires_conversation_alert=False,
        )
    }


def mutation_resolution_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    output = state.get("current_model_output", {})
    resolution = state.get("context", {}).get("mutation_resolution")
    supervisor_calls = state.get("all_supervisor_tool_calls", [])
    supervisor_results = state.get("all_supervisor_tool_results", [])
    delegated_responses = state.get("delegated_responses", [])
    human_summary = state.get("current_final_text", "").strip()
    specialist_payload: dict[str, Any] = {}
    for response in delegated_responses:
        specialist_payload.update(response.structured_payload)
    specialist_calls, specialist_results, specialist_messages = (
        _specialist_tool_payloads(delegated_responses)
    )
    direct_calls = state.get("direct_tool_calls", [])
    direct_results = state.get("direct_tool_results", [])
    tool_calls = [*specialist_calls, *direct_calls]
    tool_results = [
        *specialist_results,
        *(result.model_dump(mode="json") for result in direct_results),
    ]
    timings = state.get("mutation_resolution_llm_timings_ms", [])
    routing_mode = (
        "mutation_resolution_continuation"
        if supervisor_calls
        else "mutation_resolution_finalization"
    )
    delegated_agents = [response.agent_name for response in delegated_responses]
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="mutation_resolution",
            structured_payload={
                **specialist_payload,
                "routing_mode": routing_mode,
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "mutation_resolution": (
                    resolution if isinstance(resolution, dict) else {}
                ),
                "model_output": output,
                "supervisor_tool_calls": supervisor_calls,
                "supervisor_tool_results": [
                    result.model_dump(mode="json") for result in supervisor_results
                ],
                "delegated_agents": delegated_agents,
                "specialist_agent": (
                    delegated_responses[-1].agent_name
                    if delegated_responses
                    else ""
                ),
                "specialist_tool_calls": specialist_calls,
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "tool_messages": specialist_messages,
                "tools_executed": bool(tool_calls),
                "final_answer_source": "agent_loop_final_text",
                "message_flow": supervisor_message_flow(
                    state.get("tool_round_result_counts", [])
                ),
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "finalization_mode": "mutation_resolution_iterative_llm",
                "node_timings_ms": {"mutation_resolution_llm": sum(timings)},
                "node_timing_samples_ms": {
                    "mutation_resolution_llm": timings
                },
                "iterations": state.get("iterations", 0),
            },
            human_summary=human_summary,
            requires_conversation_alert=any(
                response.requires_conversation_alert
                for response in delegated_responses
            ),
            validation_passed=all(
                response.validation_passed for response in delegated_responses
            ),
            validation_errors=[
                error
                for response in delegated_responses
                for error in (response.validation_errors or [])
            ],
        )
    }


def confirmation_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
    proposal = mutation_confirmation_from_state(state)
    ai_message = state["confirmation_ai_message"]
    output = state.get("confirmation_model_output", {})
    human_summary, final_answer_source = required_llm_summary(
        ai_message,
        output,
        trace_id=state["trace_id"],
        decision_type="mutation_confirmation_required",
        finalized=True,
    )
    specialist_calls, specialist_results, specialist_messages = (
        _specialist_tool_payloads(state.get("delegated_responses", []))
    )
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="mutation_confirmation_required",
            structured_payload={
                "routing_mode": "mutation_confirmation_required",
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "supervisor_tool_calls": state.get(
                    "all_supervisor_tool_calls",
                    [],
                ),
                "supervisor_tool_results": [
                    result.model_dump(mode="json")
                    for result in state.get("all_supervisor_tool_results", [])
                ],
                "specialist_tool_calls": specialist_calls,
                "tool_calls": specialist_calls,
                "tool_results": specialist_results,
                "tool_messages": specialist_messages,
                "mutation_confirmation_required": True,
                "mutation_confirmation": proposal,
                "model_output": state.get("initial_model_output", {}),
                "final_model_output": output,
                "final_answer_source": final_answer_source,
                "message_flow": supervisor_message_flow(
                    state.get("tool_round_result_counts", [])
                ),
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "finalization_mode": "confirmation_llm_without_tools",
                "iterations": state.get("iterations", 0),
            },
            human_summary=human_summary,
            requires_conversation_alert=False,
        )
    }


def final_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
    if state.get("delegated_responses"):
        return _delegated_response(state)
    if state.get("direct_tool_calls"):
        return _direct_tool_response(state)
    return _direct_answer_response(state)


def continuation_required_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    continuation = state.get("continuation_type", "")
    pending_calls = state.get("pending_tool_calls", [])
    executed_calls = state.get("all_supervisor_tool_calls", [])
    results = state.get("all_supervisor_tool_results", [])
    all_calls = [*executed_calls, *pending_calls]
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="continuation_required",
            structured_payload={
                "routing_mode": "direct_tool_continuation",
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "model_output": state.get("initial_model_output", {}),
                "final_model_output": state.get("current_model_output", {}),
                "tool_calls": all_calls,
                "supervisor_tool_calls": all_calls,
                "tool_results": [
                    result.model_dump(mode="json") for result in results
                ],
                "tools_executed": bool(executed_calls),
                "message_flow": supervisor_message_flow(
                    state.get("tool_round_result_counts", []),
                    pending_tool_call=True,
                ),
                "continuation_required": True,
                "continuation_type": continuation,
                "policy_confirmation_required": has_deferred_policy_tool_call(
                    pending_calls
                ),
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "iterations": state.get("iterations", 0),
            },
            human_summary="",
            requires_conversation_alert=False,
        )
    }


def max_iterations_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    executed_calls = state.get("all_supervisor_tool_calls", [])
    results = state.get("all_supervisor_tool_results", [])
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="tool_loop_limit_exceeded",
            structured_payload={
                "routing_mode": "supervisor_max_iterations",
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "supervisor_tool_calls": executed_calls,
                "pending_tool_calls": state.get("pending_tool_calls", []),
                "model_output": state.get("initial_model_output", {}),
                "final_model_output": state.get("current_model_output", {}),
                **tool_calls_payload(executed_calls, results),
                "message_flow": supervisor_message_flow(
                    state.get("tool_round_result_counts", []),
                    pending_tool_call=True,
                ),
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "iterations": state.get("iterations", 0),
            },
            human_summary=(
                "요청 처리 중 도구 실행 한도에 도달했습니다. "
                "요청을 나누어 다시 시도해 주세요."
            ),
            requires_conversation_alert=False,
        )
    }


def _direct_answer_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    output = state.get("current_model_output", {})
    final_text = state.get("current_final_text", "").strip()
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="system_guidance",
            structured_payload={
                "routing_mode": "direct_answer",
                "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
                "executed_by": MULTITURN_CHAT_AGENT_NAME,
                "supervisor_tool_calls": [],
                "model_output": output,
                "tool_calls": [],
                "tool_results": [],
                "tools_executed": False,
                "final_answer_source": "agent_loop_final_text",
                "finalization_mode": ITERATIVE_FINALIZATION_MODE,
                "message_flow": ["HumanMessage", "AIMessage(final_answer)"],
                "agent_graph_mode": TOOL_LOOP_MODE,
                "tool_loop_mode": TOOL_LOOP_MODE,
                "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
                "iterations": 0,
            },
            human_summary=final_text,
            requires_conversation_alert=False,
        )
    }


def _direct_tool_response(
    state: MultiturnGraphState,
) -> dict[str, AgentResponse]:
    executed_calls = state.get("direct_tool_calls", [])
    results = state.get("direct_tool_results", [])
    all_tool_messages = state.get("all_tool_messages", [])
    structured_payload = {
        "routing_mode": "direct_tool",
        "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
        "executed_by": MULTITURN_CHAT_AGENT_NAME,
        "supervisor_tool_calls": state.get("all_supervisor_tool_calls", []),
        "model_output": state.get("initial_model_output", {}),
        **tool_calls_payload(executed_calls, results),
        "tool_messages": [
            {
                "name": message.name,
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            }
            for message in all_tool_messages
        ],
        "final_model_output": state.get("current_model_output", {}),
        "message_flow": supervisor_message_flow(
            state.get("tool_round_result_counts", [])
        ),
        "policy_confirmation_required": has_deferred_policy_tool_call(
            executed_calls
        ),
        "agent_graph_mode": TOOL_LOOP_MODE,
        "tool_loop_mode": TOOL_LOOP_MODE,
        "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
        "finalization_mode": ITERATIVE_FINALIZATION_MODE,
        "iterations": state.get("iterations", 0),
        "final_answer_source": "agent_loop_final_text",
    }
    return {
        "response": AgentResponse(
            trace_id=state["trace_id"],
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type="tool_call",
            structured_payload=structured_payload,
            human_summary=state.get("current_final_text", "").strip(),
            requires_conversation_alert=False,
        )
    }


def _delegated_response(state: MultiturnGraphState) -> dict[str, AgentResponse]:
    delegated_responses = state.get("delegated_responses", [])
    delegation_calls = state.get("delegation_calls", [])
    last_response = delegated_responses[-1]
    last_call = delegation_calls[-1]

    structured: dict[str, Any] = {}
    specialist_calls: list[dict[str, Any]] = []
    specialist_results: list[dict[str, Any]] = []
    specialist_messages: list[dict[str, Any]] = []
    for response in delegated_responses:
        response_structured = response.structured_payload
        structured.update(response_structured)
        calls = response_structured.get("tool_calls")
        if isinstance(calls, list):
            specialist_calls.extend(
                call for call in calls if isinstance(call, dict)
            )
        results = response_structured.get("tool_results")
        if isinstance(results, list):
            specialist_results.extend(
                result for result in results if isinstance(result, dict)
            )
        messages = response_structured.get("tool_messages")
        if isinstance(messages, list):
            specialist_messages.extend(
                message for message in messages if isinstance(message, dict)
            )

    decision_type = last_response.decision_type
    if decision_type == "continuation_required":
        final_summary = ""
        final_answer_source = "internal_control"
    else:
        final_summary = state.get("current_final_text", "").strip()
        if final_summary:
            final_answer_source = "agent_loop_final_text"
        elif has_structured_ui_response(last_response):
            final_summary = last_response.human_summary
            final_answer_source = "specialist_handoff"
        else:
            final_answer_source = "missing"

    if decision_type != "continuation_required" and any(
        str(call.get("name") or "") in SIDE_EFFECT_TOOLS
        for call in specialist_calls
    ):
        decision_type = "side_effect_assessment"

    supervisor_calls = state.get("all_supervisor_tool_calls", [])
    supervisor_results = state.get("all_supervisor_tool_results", [])
    delegation_results = [
        result
        for call, result in zip(supervisor_calls, supervisor_results, strict=True)
        if is_delegation_tool_call(call)
    ]
    structured.update(
        {
            "routing_mode": "delegated_agent",
            "supervisor_agent": MULTITURN_CHAT_AGENT_NAME,
            "specialist_agent": last_response.agent_name,
            "executed_by": MULTITURN_CHAT_AGENT_NAME,
            "delegated_agent": last_response.agent_name,
            "delegated_agents": [
                response.agent_name for response in delegated_responses
            ],
            "delegation_reason": delegation_reason(last_call),
            "delegation_reasons": [
                delegation_reason(call) for call in delegation_calls
            ],
            "delegated_by": MULTITURN_CHAT_AGENT_NAME,
            "supervisor_tool_calls": supervisor_calls,
            "supervisor_tool_results": [
                result.model_dump(mode="json") for result in supervisor_results
            ],
            "specialist_tool_calls": specialist_calls,
            "tool_calls": specialist_calls,
            "tool_results": specialist_results,
            "tools_executed": bool(specialist_calls),
            "tool_messages": specialist_messages,
            "supervisor_model_output": state.get("initial_model_output", {}),
            "supervisor_final_model_output": state.get(
                "current_model_output",
                {},
            ),
            "final_answer_source": final_answer_source,
            "agent_graph_mode": TOOL_LOOP_MODE,
            "tool_loop_mode": TOOL_LOOP_MODE,
            "tool_execution_mode": ITERATIVE_TOOL_EXECUTION_MODE,
            "finalization_mode": ITERATIVE_FINALIZATION_MODE,
            "iterations": state.get("iterations", 0),
            "message_flow": supervisor_message_flow(
                state.get("tool_round_result_counts", [])
            ),
        }
    )
    if len(specialist_calls) == 1:
        structured["tool_call"] = specialist_calls[0]
    else:
        structured.pop("tool_call", None)
    if delegation_results:
        structured["delegation_tool_result"] = delegation_results[-1].model_dump(
            mode="json"
        )
        structured["delegation_tool_results"] = [
            result.model_dump(mode="json") for result in delegation_results
        ]

    return {
        "response": AgentResponse(
            trace_id=last_response.trace_id,
            agent_name=MULTITURN_CHAT_AGENT_NAME,
            prompt_version_id=PROMPT_VERSION_ID,
            decision_type=decision_type,
            structured_payload=structured,
            human_summary=final_summary,
            requires_conversation_alert=any(
                response.requires_conversation_alert
                for response in delegated_responses
            ),
            validation_passed=all(
                response.validation_passed for response in delegated_responses
            ),
            validation_errors=[
                error
                for response in delegated_responses
                for error in (response.validation_errors or [])
            ],
        )
    }


def supervisor_message_flow(
    tool_round_result_counts: list[int],
    *,
    pending_tool_call: bool = False,
) -> list[str]:
    flow = ["HumanMessage"]
    for result_count in tool_round_result_counts:
        flow.append("AIMessage(tool_calls)")
        flow.extend("ToolMessage" for _ in range(max(1, result_count)))
    if pending_tool_call:
        flow.append("AIMessage(tool_calls)")
    else:
        flow.append("AIMessage(final_answer)")
    return flow


def mutation_confirmation_from_state(
    state: MultiturnGraphState,
) -> dict[str, Any]:
    for response in state.get("delegated_responses", []):
        proposal = response.structured_payload.get("mutation_confirmation")
        if isinstance(proposal, dict) and proposal:
            return proposal
    for result in state.get("all_supervisor_tool_results", []):
        response = result.response if isinstance(result.response, dict) else {}
        proposal = response.get("mutation_confirmation")
        if isinstance(proposal, dict) and proposal:
            return proposal
        specialist = response.get("structured_payload")
        if isinstance(specialist, dict):
            proposal = specialist.get("mutation_confirmation")
            if isinstance(proposal, dict) and proposal:
                return proposal
    return {}


def _specialist_tool_payloads(
    responses: list[AgentResponse],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    calls: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    for response in responses:
        structured = response.structured_payload
        response_calls = structured.get("tool_calls")
        if isinstance(response_calls, list):
            calls.extend(item for item in response_calls if isinstance(item, dict))
        response_results = structured.get("tool_results")
        if isinstance(response_results, list):
            results.extend(
                item for item in response_results if isinstance(item, dict)
            )
        response_messages = structured.get("tool_messages")
        if isinstance(response_messages, list):
            messages.extend(
                item for item in response_messages if isinstance(item, dict)
            )
    return calls, results, messages
