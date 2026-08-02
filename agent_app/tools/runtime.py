from __future__ import annotations

from time import perf_counter
from typing import Any

from agent_app import trace_logging
from agent_app.integration.backend_client import (
    backend_request_attempt_count,
    reset_backend_request_attempt_count,
)
from agent_app.observability.tool_calls import record_tool_call
from agent_app.tools.budget import (
    current_tool_execution_budget,
    tool_execution_budget_scope,
)
from agent_app.tools.policy_gate import (
    ToolCallContext,
    ToolCallOrigin,
    ToolPolicyGate,
)
from agent_app.tools.protocol import AgentToolExecutorProtocol
from agent_app.tools.side_effects import (
    ae_tool_calls_from_lookup,
    positive_side_effect_lookup,
)
from shared.redaction import safe_log_arguments
from shared.schemas import ToolCallResult
from shared.time_utils import utc_now
from shared.tool_names import (
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)


def _tool_call_log_payload(tool_call: dict[str, Any]) -> dict[str, Any]:
    arguments = tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {}
    return {
        "tool": str(tool_call.get("name") or ""),
        "arguments": safe_log_arguments(arguments),
    }


def _tool_result_log_payload(result: Any) -> dict[str, Any]:
    response = result.response if isinstance(getattr(result, "response", None), dict) else {}
    payload: dict[str, Any] = {
        "tool": str(getattr(result, "tool_name", "")),
        "status": str(getattr(result, "status", "")),
        "elapsed_ms": max(
            0,
            int(getattr(result, "elapsed_ms", 0) or 0),
        ),
        "attempt_count": max(
            1,
            int(getattr(result, "attempt_count", 1) or 1),
        ),
        "retryable": bool(getattr(result, "retryable", False)),
        "error": trace_logging.snippet(getattr(result, "error", "")),
        "idempotency_key_present": bool(getattr(result, "idempotency_key", None)),
    }
    if result.tool_name == GET_MEDICATION_SIDE_EFFECT_ASSESSMENT:
        payload["response"] = {
            "suspected": response.get("suspected"),
            "matched_effects": response.get("matched_effects", []),
            "matched_items": response.get("matched_items", []),
            "severity": response.get("severity"),
            "evidence": trace_logging.snippet(response.get("evidence")),
        }
    elif result.tool_name == GET_PRO_CTCAE_QUESTIONNAIRE:
        payload["response"] = {
            "input_symptom": trace_logging.snippet(response.get("input_symptom")),
            "matched": response.get("matched"),
            "match_type": response.get("match_type"),
            "matched_symptom_term": response.get("matched_symptom_term"),
            "matched_korean_symptom_name": response.get("matched_korean_symptom_name"),
            "similarity": response.get("similarity"),
            "question_count": len(response.get("questions", [])) if isinstance(response.get("questions"), list) else 0,
        }
    elif result.tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        payload["response"] = {
            "dose_event_id": response.get("dose_event_id"),
            "status": response.get("status"),
            "message": trace_logging.snippet(response.get("message")),
        }
    else:
        payload["response_keys"] = sorted(str(key) for key in response.keys())
    return payload


def _routing_log_payload(routing_context: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(routing_context, dict):
        return {}
    payload: dict[str, Any] = {}
    for key in (
        "routing_mode",
        "executed_by",
        "supervisor_agent",
        "specialist_agent",
        "delegated_agent",
        "delegated_by",
        "delegation_reason",
        "tool_loop_mode",
        "agent_graph_mode",
        "tool_execution_mode",
        "finalization_mode",
    ):
        value = routing_context.get(key)
        if value is not None:
            payload[key] = str(value)
    for key in ("supervisor_tool_names", "specialist_tool_names", "tool_names"):
        value = routing_context.get(key)
        if isinstance(value, list):
            payload[key] = [str(item) for item in value]
    return payload


class ToolRuntime:
    def __init__(
        self,
        executor: AgentToolExecutorProtocol | None,
        *,
        policy_gate: ToolPolicyGate | None = None,
    ) -> None:
        self.executor = executor
        self.policy_gate = policy_gate or ToolPolicyGate()

    async def execute(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        force_ae_after_positive_lookup: bool = False,
        routing_context: dict[str, Any] | None = None,
        call_context: ToolCallContext | None = None,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        if self.executor is None or not tool_calls:
            return tool_calls, []
        with tool_execution_budget_scope() as budget:
            agent_name = str((routing_context or {}).get("executed_by") or source_event_type or "agent_app")
            decision_type = str((routing_context or {}).get("routing_mode") or "tool_call")
            budget.validate_turn_plan(
                tool_calls,
                trace_id=trace_id,
                agent_name=agent_name,
                decision_type=decision_type,
            )
            budget.ensure_capacity(
                len(tool_calls),
                trace_id=trace_id,
                agent_name=agent_name,
                decision_type=decision_type,
            )
            return await self._execute_with_budget(
                tool_calls,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
                force_ae_after_positive_lookup=(force_ae_after_positive_lookup),
                routing_context=routing_context,
                call_context=call_context,
            )

    async def _execute_with_budget(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        force_ae_after_positive_lookup: bool = False,
        routing_context: dict[str, Any] | None = None,
        call_context: ToolCallContext | None = None,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        budget = current_tool_execution_budget()
        initial_context = call_context or ToolCallContext()
        executed_calls = list(tool_calls)
        call_contexts = [initial_context for _ in executed_calls]
        results = []
        ae_requested_symptoms = {
            str((call.get("arguments") or {}).get("symptom_text") or "")
            .strip()
            .casefold()
            for call in executed_calls
            if call.get("name") == GET_PRO_CTCAE_QUESTIONNAIRE
            and isinstance(call.get("arguments"), dict)
        }
        routing = _routing_log_payload(routing_context)
        trace_logging.log_info(
            "agent_tool_plan_created",
            trace_id=trace_id,
            source_event_type=source_event_type,
            routing=routing,
            tool_count=len(executed_calls),
            tool_names=[str(call.get("name") or "") for call in executed_calls],
            tool_calls=[_tool_call_log_payload(call) for call in executed_calls],
        )
        index = 0
        while index < len(executed_calls):
            tool_call = executed_calls[index]
            current_context = call_contexts[index]
            budget.consume_tool_call(
                str(tool_call.get("name") or "unknown"),
                trace_id=trace_id,
                agent_name=str((routing_context or {}).get("executed_by") or source_event_type or "agent_app"),
                decision_type=str((routing_context or {}).get("routing_mode") or "tool_call"),
            )
            started_at = utc_now()
            trace_logging.log_info(
                "agent_tool_call_started",
                trace_id=trace_id,
                source_event_type=source_event_type,
                routing=routing,
                tool_index=index,
                tool_call_origin=current_context.origin.value,
                started_at=started_at.isoformat(),
                **_tool_call_log_payload(tool_call),
            )
            started = perf_counter()
            reset_backend_request_attempt_count()
            result = self.policy_gate.authorize(
                tool_call,
                trace_id=trace_id,
                source_event_type=source_event_type,
                payload=payload,
                context=current_context,
            )
            if result is None:
                result = await self.executor.execute_tool_call(
                    tool_call,
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                    payload=payload,
                )
            elapsed_ms = max(
                0,
                round((perf_counter() - started) * 1000),
            )
            response = result.response if isinstance(getattr(result, "response", None), dict) else {}
            response_error = response.get("error") if isinstance(response.get("error"), dict) else {}
            result = result.model_copy(
                update={
                    "elapsed_ms": max(
                        elapsed_ms,
                        int(getattr(result, "elapsed_ms", 0) or 0),
                    ),
                    "attempt_count": max(
                        1,
                        int(getattr(result, "attempt_count", 1) or 1),
                        backend_request_attempt_count(),
                    ),
                    "retryable": bool(getattr(result, "retryable", False) or response_error.get("retryable") is True),
                }
            )
            completed_at = utc_now()
            results.append(result)
            record_tool_call(
                trace_id=trace_id,
                source_event_type=source_event_type,
                tool_index=index,
                tool_call_origin=current_context.origin.value,
                routing=routing,
                call=tool_call,
                result=result.model_dump(mode="json"),
                started_at=started_at,
                completed_at=completed_at,
                latency_ms=result.elapsed_ms,
            )
            trace_logging.log_info(
                "agent_tool_call_completed",
                trace_id=trace_id,
                source_event_type=source_event_type,
                routing=routing,
                tool_index=index,
                tool_call_origin=current_context.origin.value,
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
                **_tool_result_log_payload(result),
            )
            input_required = result.status == "success" and result.response.get("input_required") is True
            selection_required = result.status == "success" and result.response.get("selection_required") is True
            if (
                result.status == "confirmation_required"
                or input_required
                or selection_required
            ):
                if selection_required:
                    block_reason = "blocked_by_required_selection"
                elif input_required:
                    block_reason = "blocked_by_required_input"
                else:
                    block_reason = "blocked_by_pending_confirmation"
                for blocked_index in range(index + 1, len(executed_calls)):
                    blocked_call = executed_calls[blocked_index]
                    blocked_result = ToolCallResult(
                        tool_name=str(blocked_call.get("name") or "unknown"),
                        status="skipped",
                        response={
                            "reason": block_reason,
                            "blocking_tool": result.tool_name,
                        },
                        error=block_reason,
                        idempotency_key=f"{trace_id}:{blocked_call.get('name') or 'unknown'}:blocked:{blocked_index}",
                    )
                    results.append(blocked_result)
                    blocked_at = utc_now()
                    record_tool_call(
                        trace_id=trace_id,
                        source_event_type=source_event_type,
                        tool_index=blocked_index,
                        tool_call_origin=current_context.origin.value,
                        routing=routing,
                        call=blocked_call,
                        result=blocked_result.model_dump(mode="json"),
                        started_at=blocked_at,
                        completed_at=blocked_at,
                        latency_ms=0,
                    )
                    trace_logging.log_info(
                        "agent_tool_call_blocked_by_terminal_result",
                        trace_id=trace_id,
                        source_event_type=source_event_type,
                        routing=routing,
                        tool_index=blocked_index,
                        terminal_tool=result.tool_name,
                        terminal_reason=block_reason,
                        **_tool_call_log_payload(blocked_call),
                    )
                break
            if (
                force_ae_after_positive_lookup
                and positive_side_effect_lookup(result)
            ):
                for ae_call in ae_tool_calls_from_lookup(
                    result,
                    source_tool_call=tool_call,
                    payload=payload,
                ):
                    ae_arguments = ae_call.get("arguments")
                    symptom_key = (
                        str(ae_arguments.get("symptom_text") or "")
                        .strip()
                        .casefold()
                        if isinstance(ae_arguments, dict)
                        else ""
                    )
                    if not symptom_key or symptom_key in ae_requested_symptoms:
                        continue
                    trace_logging.log_info(
                        "agent_tool_call_forced",
                        trace_id=trace_id,
                        source_event_type=source_event_type,
                        routing=routing,
                        reason="positive_side_effect_lookup",
                        source_tool=str(tool_call.get("name") or ""),
                        forced_tool=GET_PRO_CTCAE_QUESTIONNAIRE,
                        arguments=(
                            _tool_call_log_payload(ae_call).get(
                                "arguments", {}
                            )
                        ),
                    )
                    executed_calls.append(ae_call)
                    call_contexts.append(
                        ToolCallContext(
                            origin=ToolCallOrigin.SAFETY_RULE,
                            prior_call_fingerprints=(
                                initial_context.prior_call_fingerprints
                            ),
                        )
                    )
                    ae_requested_symptoms.add(symptom_key)
            index += 1
        return executed_calls, results
