from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object
from shared.redaction import redact_inline_secrets, stable_hash
from system_app.models import AgentRunStep, AgentRunTrace

TRACE_REPLAY_OUTPUT_DIR = Path("outputs/replays")


def build_trace_replay_detail(session: Session, trace_id: str) -> dict[str, Any] | None:
    trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == trace_id))
    if trace is None:
        return None
    steps = list(session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id == trace.trace_id).order_by(AgentRunStep.id.asc())).all())
    metadata = parse_json_object(trace.metadata_json)
    step_views = [_step_view(step) for step in steps]
    replay_inputs = {
        "trace_id": trace.trace_id,
        "request_id": trace.request_id or "",
        "workflow_name": trace.workflow_name or "",
        "source_event_type": trace.source_event_type or "",
        "input_hash": trace.input_hash or "",
        "output_hash": trace.output_hash or "",
        "patient_id_hash_present": bool(trace.patient_id_hash),
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
    }
    return {
        "trace_id": trace.trace_id,
        "short_trace_id": _short_id(trace.trace_id),
        "workflow_name": trace.workflow_name or "-",
        "source_event_type": trace.source_event_type or "-",
        "status": trace.status or "-",
        "agent_name": trace.agent_name or "-",
        "decision_type": trace.decision_type or "-",
        "prompt_version_id": trace.prompt_version_id or "-",
        "provider": trace.provider or "-",
        "model_tier": trace.model_tier or "-",
        "model_id": trace.model_id or "-",
        "latency_ms": trace.latency_ms,
        "tool_count": trace.tool_count,
        "cost_label": f"${trace.estimated_cost_usd:.6f}",
        "error_message": redact_inline_secrets(trace.error_message or "", limit=240),
        "updated_at": trace.updated_at.isoformat() if trace.updated_at else "",
        "replay_ready": bool(trace.input_hash or trace.request_id or step_views),
        "replay_inputs": replay_inputs,
        "replay_plan": _replay_plan(trace, step_views),
        "steps": step_views,
        "eval_seed": {
            "source_type": "trace",
            "trace_id": trace.trace_id,
            "suggested_case_id": f"prod-trace-{stable_hash(trace.trace_id)[:12]}",
        },
    }


def write_trace_replay_artifact(session: Session, trace_id: str) -> dict[str, Any]:
    detail = build_trace_replay_detail(session, trace_id)
    if detail is None:
        return {
            "success": False,
            "action": "trace_replay_artifact",
            "trace_id": trace_id,
            "message": "agent trace not found",
        }
    safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in trace_id)[:120] or stable_hash(trace_id)[:12]
    output_path = TRACE_REPLAY_OUTPUT_DIR / f"{safe_name}.json"
    payload = {
        "artifact_type": "trace_replay",
        "artifact_version": "v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trace": detail,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        "action": "trace_replay_artifact",
        "trace_id": trace_id,
        "artifact_path": str(output_path),
        "message": "trace replay artifact written",
    }


def _step_view(step: AgentRunStep) -> dict[str, Any]:
    metadata = parse_json_object(step.metadata_json)
    return {
        "step_type": step.step_type or "-",
        "step_name": step.step_name or step.tool_name or "-",
        "status": step.status or "-",
        "latency_ms": step.latency_ms,
        "tool_name": step.tool_name or "",
        "side_effect_level": step.side_effect_level or "",
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
        "metadata_preview": _metadata_preview(metadata),
    }


def _metadata_preview(metadata: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key, value in sorted(metadata.items()):
        if isinstance(value, dict):
            pieces.append(f"{key}=keys:{','.join(sorted(str(item) for item in value.keys())[:6])}")
        elif isinstance(value, list):
            pieces.append(f"{key}=count:{len(value)}")
        else:
            pieces.append(f"{key}={redact_inline_secrets(str(value), limit=80)}")
    return " · ".join(pieces[:8]) if pieces else "-"


def _replay_plan(trace: AgentRunTrace, steps: list[dict[str, Any]]) -> list[str]:
    plan = [
        "Use the redacted trace identifiers and metadata keys as replay inputs.",
        "Run deterministic gates first, then semantic/behavioral review if the trace included model or tool steps.",
    ]
    if trace.status == "failed" or trace.error_message:
        plan.append("Keep this trace in eval backlog until owner review and regression evidence are attached.")
    if any(step["side_effect_level"] in {"write", "candidate_write"} for step in steps):
        plan.append("Confirm high-risk or write-capable tool steps are replayed with human approval gates enabled.")
    if any(step["step_type"] == "tool_call" for step in steps):
        plan.append("Verify tool source labels, side-effect level, and freshness probe before replay sign-off.")
    return plan


def _short_id(value: str) -> str:
    if len(value) <= 16:
        return value
    return f"{value[:8]}...{value[-6:]}"
