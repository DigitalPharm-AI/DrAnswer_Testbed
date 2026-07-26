from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    POLICY_TOOLS,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
    canonical_tool_name,
)
from shared.json_utils import dump_json
from shared.readiness_budget import estimate_model_cost_usd
from shared.redaction import redact_for_logging, redact_inline_secrets, stable_hash
from shared.schemas import AgentResponse
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import AgentRunStep, AgentRunTrace


def upsert_agent_run_trace(
    session: Session,
    response: AgentResponse,
    *,
    workflow_name: str,
    source_event_type: str,
    status: str = "completed",
    request_id: str = "",
    patient_id: str | None = None,
    request_message: str = "",
    notification_id: int | None = None,
    job_id: int | None = None,
    related_dose_event_id: int | None = None,
    error_message: str = "",
) -> AgentRunTrace:
    settings = get_settings()
    structured = response.structured_payload if isinstance(response.structured_payload, dict) else {}
    token_usage = _token_usage(structured)
    input_tokens = token_usage["input_tokens"]
    output_tokens = token_usage["output_tokens"]
    trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == response.trace_id))
    now = utc_now()
    if trace is None:
        trace = AgentRunTrace(trace_id=response.trace_id, created_at=now)
        session.add(trace)

    trace.request_id = request_id or trace.request_id or ""
    trace.patient_id_hash = stable_hash(patient_id or "") if patient_id else trace.patient_id_hash or ""
    trace.workflow_name = workflow_name
    trace.source_event_type = source_event_type
    trace.status = status
    trace.agent_name = response.agent_name
    trace.decision_type = response.decision_type
    trace.prompt_version_id = response.prompt_version_id
    trace.provider = str(structured.get("provider") or settings.llm_provider)
    trace.model_tier = str(structured.get("model_tier") or settings.llm_model_tier)
    trace.model_id = str(structured.get("model_id") or settings.model_id_for_tier(trace.model_tier))
    trace.input_hash = stable_hash(request_message) if request_message else trace.input_hash or ""
    trace.output_hash = stable_hash(response.human_summary or "")
    trace.input_tokens = input_tokens
    trace.output_tokens = output_tokens
    trace.estimated_cost_usd = estimate_model_cost_usd(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_usd_per_1m_tokens=settings.agent_cost_input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=settings.agent_cost_output_usd_per_1m_tokens,
    )
    trace.latency_ms = _int_value(structured.get("elapsed_ms") or structured.get("latency_ms"))
    trace.tool_count = len(_tool_calls(structured))
    trace.error_message = redact_inline_secrets(error_message, limit=1000)
    trace.metadata_json = dump_json(
        redact_for_logging(
            {
                "notification_id": notification_id,
                "job_id": job_id,
                "related_dose_event_id": related_dose_event_id,
                "validation_passed": response.validation_passed,
                "validation_errors": response.validation_errors,
                "structured_payload_keys": sorted(str(key) for key in structured.keys()),
                "routing": _routing_metadata(structured, response.agent_name),
                "human_summary": response.human_summary,
                "request_message": request_message,
                "token_usage": token_usage,
            }
        )
    )
    trace.completed_at = now if status in {"completed", "failed", "answered", "applied"} else None
    trace.updated_at = now
    session.flush()

    _replace_steps(session, response, source_event_type=source_event_type, status=status)
    return trace


def record_agent_run_failure(
    session: Session,
    *,
    trace_id: str | None,
    workflow_name: str,
    source_event_type: str,
    request_id: str = "",
    patient_id: str | None = None,
    request_message: str = "",
    agent_name: str | None = None,
    decision_type: str | None = None,
    error_message: str = "",
    job_id: int | None = None,
    notification_id: int | None = None,
    related_dose_event_id: int | None = None,
) -> AgentRunTrace | None:
    if not trace_id:
        return None
    settings = get_settings()
    now = utc_now()
    trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == trace_id))
    if trace is None:
        trace = AgentRunTrace(trace_id=trace_id, created_at=now)
        session.add(trace)
    trace.request_id = request_id or trace.request_id or ""
    trace.patient_id_hash = stable_hash(patient_id or "") if patient_id else trace.patient_id_hash or ""
    trace.workflow_name = workflow_name
    trace.source_event_type = source_event_type
    trace.status = "failed"
    trace.agent_name = agent_name or trace.agent_name or ""
    trace.decision_type = decision_type or trace.decision_type or "agent_async_failure"
    trace.provider = settings.llm_provider
    trace.model_tier = settings.llm_model_tier
    trace.model_id = settings.model_id_for_tier(settings.llm_model_tier)
    trace.input_hash = stable_hash(request_message) if request_message else trace.input_hash or ""
    trace.output_hash = ""
    trace.input_tokens = 0
    trace.output_tokens = 0
    trace.estimated_cost_usd = 0.0
    trace.latency_ms = 0
    trace.tool_count = 0
    trace.error_message = redact_inline_secrets(error_message, limit=1000)
    trace.metadata_json = dump_json(
        redact_for_logging(
            {
                "notification_id": notification_id,
                "job_id": job_id,
                "related_dose_event_id": related_dose_event_id,
                "request_message": request_message,
                "error_message": error_message,
            }
        )
    )
    trace.completed_at = now
    trace.updated_at = now
    session.flush()

    session.execute(delete(AgentRunStep).where(AgentRunStep.trace_id == trace_id))
    session.add(
        AgentRunStep(
            trace_id=trace_id,
            step_type="model_call",
            step_name=trace.agent_name,
            status="failed",
            metadata_json=dump_json(
                redact_for_logging(
                    {
                        "source_event_type": source_event_type,
                        "decision_type": trace.decision_type,
                        "error_message": error_message,
                    }
                )
            ),
        )
    )
    session.add(
        AgentRunStep(
            trace_id=trace_id,
            step_type="final_response",
            step_name=trace.decision_type,
            status="failed",
            metadata_json=dump_json(redact_for_logging({"error_message": error_message})),
        )
    )
    session.flush()
    return trace


def agent_trace_payload(trace: AgentRunTrace, steps: list[AgentRunStep] | None = None) -> dict[str, Any]:
    return {
        "trace_id": trace.trace_id,
        "request_id": trace.request_id,
        "patient_id_hash": trace.patient_id_hash,
        "workflow_name": trace.workflow_name,
        "source_event_type": trace.source_event_type,
        "status": trace.status,
        "agent_name": trace.agent_name,
        "decision_type": trace.decision_type,
        "prompt_version_id": trace.prompt_version_id,
        "provider": trace.provider,
        "model_tier": trace.model_tier,
        "model_id": trace.model_id,
        "input_hash": trace.input_hash,
        "output_hash": trace.output_hash,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "estimated_cost_usd": trace.estimated_cost_usd,
        "latency_ms": trace.latency_ms,
        "tool_count": trace.tool_count,
        "error_message": trace.error_message,
        "metadata": trace.metadata_json,
        "created_at": trace.created_at.isoformat() if trace.created_at else None,
        "updated_at": trace.updated_at.isoformat() if trace.updated_at else None,
        "steps": [agent_trace_step_payload(step) for step in steps or []],
    }


def agent_trace_step_payload(step: AgentRunStep) -> dict[str, Any]:
    return {
        "trace_id": step.trace_id,
        "step_type": step.step_type,
        "step_name": step.step_name,
        "status": step.status,
        "latency_ms": step.latency_ms,
        "tool_name": step.tool_name,
        "side_effect_level": step.side_effect_level,
        "metadata": step.metadata_json,
        "created_at": step.created_at.isoformat() if step.created_at else None,
    }


def _replace_steps(session: Session, response: AgentResponse, *, source_event_type: str, status: str) -> None:
    session.execute(delete(AgentRunStep).where(AgentRunStep.trace_id == response.trace_id))
    structured = response.structured_payload if isinstance(response.structured_payload, dict) else {}
    session.add(
        AgentRunStep(
            trace_id=response.trace_id,
            step_type="model_call",
            step_name=response.agent_name,
            status="completed" if status != "failed" else "failed",
            latency_ms=_int_value(structured.get("elapsed_ms") or structured.get("latency_ms")),
            metadata_json=dump_json(
                redact_for_logging(
                    {
                        "source_event_type": source_event_type,
                        "decision_type": response.decision_type,
                        "prompt_version_id": response.prompt_version_id,
                        "token_usage": _token_usage(structured),
                        "routing": _routing_metadata(structured, response.agent_name),
                    }
                )
            ),
        )
    )
    for index, tool_call in enumerate(_tool_calls(structured)):
        tool_name = str(tool_call.get("name") or "")
        result = _matching_tool_result(tool_name, _tool_results(structured), index)
        session.add(
            AgentRunStep(
                trace_id=response.trace_id,
                step_type="tool_call",
                step_name=tool_name,
                status=str(result.get("status") or "planned"),
                tool_name=tool_name,
                side_effect_level=_side_effect_level(tool_name),
                latency_ms=_int_value(result.get("elapsed_ms")),
                metadata_json=dump_json(
                    redact_for_logging(
                        {
                            "tool_index": index,
                            "routing": _routing_metadata(structured, response.agent_name),
                            "executed_by": str(structured.get("executed_by") or response.agent_name),
                            "arguments": tool_call.get("arguments") if isinstance(tool_call.get("arguments"), dict) else {},
                            "result_keys": sorted(str(key) for key in result.keys()),
                            "error": result.get("error", ""),
                        }
                    )
                ),
            )
        )
    session.add(
        AgentRunStep(
            trace_id=response.trace_id,
            step_type="final_response",
            step_name=response.decision_type,
            status=status,
            metadata_json=dump_json(
                redact_for_logging(
                    {
                        "human_summary": response.human_summary,
                        "validation_passed": response.validation_passed,
                        "validation_errors": response.validation_errors,
                        "routing": _routing_metadata(structured, response.agent_name),
                    }
                )
            ),
        )
    )
    session.flush()


def _tool_calls(structured: dict[str, Any]) -> list[dict[str, Any]]:
    calls = structured.get("tool_calls")
    if isinstance(calls, list):
        return [call for call in calls if isinstance(call, dict)]
    call = structured.get("tool_call")
    return [call] if isinstance(call, dict) else []


def _tool_results(structured: dict[str, Any]) -> list[dict[str, Any]]:
    results = structured.get("tool_results")
    return [result for result in results if isinstance(result, dict)] if isinstance(results, list) else []


def _matching_tool_result(tool_name: str, results: list[dict[str, Any]], index: int) -> dict[str, Any]:
    if index < len(results):
        candidate = results[index]
        if not tool_name or str(candidate.get("tool_name") or "") in {"", tool_name}:
            return candidate
    return next((result for result in results if str(result.get("tool_name") or "") == tool_name), {})


def _routing_metadata(structured: dict[str, Any], response_agent_name: str) -> dict[str, Any]:
    return {
        "routing_mode": str(structured.get("routing_mode") or "unknown"),
        "executed_by": str(structured.get("executed_by") or response_agent_name or ""),
        "supervisor_agent": str(structured.get("supervisor_agent") or ""),
        "specialist_agent": str(structured.get("specialist_agent") or ""),
        "delegated_agent": str(structured.get("delegated_agent") or ""),
        "delegated_by": str(structured.get("delegated_by") or ""),
        "delegation_reason": str(structured.get("delegation_reason") or ""),
        "supervisor_tool_names": _tool_names(structured.get("supervisor_tool_calls")),
        "specialist_tool_names": _tool_names(structured.get("specialist_tool_calls")),
        "tool_names": _tool_names(structured.get("tool_calls")),
    }


def _tool_names(raw_calls: Any) -> list[str]:
    if not isinstance(raw_calls, list):
        return []
    names: list[str] = []
    for call in raw_calls:
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _token_usage(structured: dict[str, Any]) -> dict[str, int]:
    usage = structured.get("token_usage") if isinstance(structured.get("token_usage"), dict) else {}
    if not usage and isinstance(structured.get("usage"), dict):
        usage = structured["usage"]
    return {
        "input_tokens": _int_value(usage.get("input_tokens") or usage.get("inputTokens") or usage.get("prompt_tokens")),
        "output_tokens": _int_value(usage.get("output_tokens") or usage.get("outputTokens") or usage.get("completion_tokens")),
    }


def _int_value(value: Any) -> int:
    try:
        return max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return 0


def _side_effect_level(tool_name: str) -> str:
    name = canonical_tool_name(tool_name)
    if name in {
        UPDATE_MEDICATION_DOSE_EVENT_STATUS,
        CREATE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
        UPSERT_NUTRITION_PREFERENCE_FACT,
        *POLICY_TOOLS,
    }:
        return "write"
    if name in {
        GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
        GET_NUTRITION_DAILY_SUMMARY,
        GET_NUTRITION_MEAL_RECORD_LIST,
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        GET_NUTRITION_PREFERENCE_SUMMARY,
        GET_PRO_CTCAE_QUESTIONNAIRE,
    }:
        return "read"
    return "none"
