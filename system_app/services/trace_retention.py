from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from shared.time_utils import utc_now
from system_app.models import AgentRunStep, AgentRunTrace

REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_RETENTION_AUDIT_PATH = REPO_ROOT / "data" / "observability" / "trace_retention_audit.json"
TRACE_RETENTION_POLICY = {
    "policy_id": "health-agent-trace-retention-v1",
    "normal_days": 30,
    "problem_days": 90,
    "mode": "summary_only",
}


def trace_retention_expired_rows(session: Session) -> list[dict[str, Any]]:
    now = utc_now()
    normal_cutoff = now - timedelta(days=TRACE_RETENTION_POLICY["normal_days"])
    problem_cutoff = now - timedelta(days=TRACE_RETENTION_POLICY["problem_days"])
    rows: list[dict[str, Any]] = []
    traces = session.query(AgentRunTrace).all()
    for trace in traces:
        anchor = trace.completed_at or trace.updated_at or trace.started_at or trace.created_at
        if anchor is None:
            continue
        is_problem = trace.status in {"failed", "error", "dead"} or bool(trace.error_message)
        cutoff = problem_cutoff if is_problem else normal_cutoff
        if anchor >= cutoff:
            continue
        rows.append(
            {
                "trace_id": trace.trace_id,
                "workflow_name": trace.workflow_name,
                "source_event_type": trace.source_event_type,
                "status": trace.status,
                "is_problem": is_problem,
                "anchor_at": anchor.isoformat(),
                "retention_days": TRACE_RETENTION_POLICY["problem_days"] if is_problem else TRACE_RETENTION_POLICY["normal_days"],
            }
        )
    return rows


def trace_retention_expired_count(session: Session) -> int:
    return len(trace_retention_expired_rows(session))


def run_trace_retention_cleanup(session: Session, *, execute: bool = False, actor: str = "LOGS") -> dict[str, Any]:
    expired_rows = trace_retention_expired_rows(session)
    trace_ids = [row["trace_id"] for row in expired_rows]
    span_count = 0
    if trace_ids:
        span_count = int(session.query(AgentRunStep).filter(AgentRunStep.trace_id.in_(trace_ids)).count())
        if execute:
            session.query(AgentRunStep).filter(AgentRunStep.trace_id.in_(trace_ids)).delete(synchronize_session=False)
    artifact = {
        "artifact_type": "trace_retention_cleanup",
        "policy": TRACE_RETENTION_POLICY,
        "mode": "execute" if execute else "dry_run",
        "actor": actor,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "expired_trace_count": len(expired_rows),
        "redacted_span_count": span_count if execute else 0,
        "candidate_span_count": span_count,
        "retained_summary_count": len(expired_rows),
        "expired_traces": expired_rows[:50],
    }
    _append_trace_retention_audit(artifact)
    message = (
        f"trace retention cleanup executed: {len(expired_rows)} traces, {span_count} spans redacted"
        if execute
        else f"trace retention cleanup dry-run recorded: {len(expired_rows)} expired traces, {span_count} candidate spans"
    )
    return {
        "success": True,
        "action": "trace_retention_cleanup",
        "artifact_path": str(TRACE_RETENTION_AUDIT_PATH),
        "trace_id": f"{len(expired_rows)} traces",
        "message": message,
        "expired_trace_count": len(expired_rows),
        "candidate_span_count": span_count,
        "redacted_span_count": span_count if execute else 0,
    }


def _append_trace_retention_audit(entry: dict[str, Any]) -> None:
    TRACE_RETENTION_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if TRACE_RETENTION_AUDIT_PATH.exists():
        try:
            existing = json.loads(TRACE_RETENTION_AUDIT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            existing = []
    else:
        existing = []
    rows = existing if isinstance(existing, list) else []
    rows.append(entry)
    TRACE_RETENTION_AUDIT_PATH.write_text(json.dumps(rows[-100:], ensure_ascii=False, indent=2), encoding="utf-8")
