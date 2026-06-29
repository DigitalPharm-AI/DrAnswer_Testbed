from __future__ import annotations

from typing import Any

from agent_app import trace_logging
from agent_app.tool_protocol import AgentToolExecutorProtocol
from agent_app.tool_side_effects import ae_tool_call_from_lookup, positive_side_effect_lookup
from shared.redaction import safe_log_arguments


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
        "error": trace_logging.snippet(getattr(result, "error", "")),
        "idempotency_key_present": bool(getattr(result, "idempotency_key", None)),
    }
    if result.tool_name == "lookup_side_effect_info":
        payload["response"] = {
            "suspected": response.get("suspected"),
            "matched_effects": response.get("matched_effects", []),
            "matched_items": response.get("matched_items", []),
            "severity": response.get("severity"),
            "evidence": trace_logging.snippet(response.get("evidence")),
        }
    elif result.tool_name == "AE_pro_ctcae":
        payload["response"] = {
            "input_symptom": trace_logging.snippet(response.get("input_symptom")),
            "matched": response.get("matched"),
            "match_type": response.get("match_type"),
            "matched_symptom_term": response.get("matched_symptom_term"),
            "matched_korean_symptom_name": response.get("matched_korean_symptom_name"),
            "similarity": response.get("similarity"),
            "question_count": len(response.get("questions", [])) if isinstance(response.get("questions"), list) else 0,
        }
    elif result.tool_name == "mark_dose_taken":
        payload["response"] = {
            "dose_event_id": response.get("dose_event_id"),
            "status": response.get("status"),
            "message": trace_logging.snippet(response.get("message")),
        }
    else:
        payload["response_keys"] = sorted(str(key) for key in response.keys())
    return payload


class ToolRuntime:
    def __init__(self, executor: AgentToolExecutorProtocol | None) -> None:
        self.executor = executor

    async def execute(
        self,
        tool_calls: list[dict[str, Any]],
        *,
        trace_id: str,
        source_event_type: str,
        payload: dict[str, Any],
        force_ae_after_positive_lookup: bool = False,
    ) -> tuple[list[dict[str, Any]], list[Any]]:
        if self.executor is None or not tool_calls:
            return tool_calls, []
        executed_calls = list(tool_calls)
        results = []
        ae_already_requested = any(call.get("name") == "AE_pro_ctcae" for call in executed_calls)
        trace_logging.log_info(
            "agent_tool_plan_created",
            trace_id=trace_id,
            source_event_type=source_event_type,
            tool_count=len(executed_calls),
            tool_names=[str(call.get("name") or "") for call in executed_calls],
            tool_calls=[_tool_call_log_payload(call) for call in executed_calls],
        )
        index = 0
        while index < len(executed_calls):
            tool_call = executed_calls[index]
            trace_logging.log_info(
                "agent_tool_call_started",
                trace_id=trace_id,
                source_event_type=source_event_type,
                tool_index=index,
                **_tool_call_log_payload(tool_call),
            )
            result = await self.executor.execute_tool_call(tool_call, trace_id=trace_id, source_event_type=source_event_type, payload=payload)
            results.append(result)
            trace_logging.log_info(
                "agent_tool_call_completed",
                trace_id=trace_id,
                source_event_type=source_event_type,
                tool_index=index,
                **_tool_result_log_payload(result),
            )
            if force_ae_after_positive_lookup and positive_side_effect_lookup(result) and not ae_already_requested:
                ae_call = ae_tool_call_from_lookup(tool_call, result, payload)
                trace_logging.log_info(
                    "agent_tool_call_forced",
                    trace_id=trace_id,
                    source_event_type=source_event_type,
                    reason="positive_side_effect_lookup",
                    source_tool=str(tool_call.get("name") or ""),
                    forced_tool="AE_pro_ctcae",
                    arguments=_tool_call_log_payload(ae_call).get("arguments", {}),
                )
                executed_calls.append(ae_call)
                ae_already_requested = True
            index += 1
        return executed_calls, results
