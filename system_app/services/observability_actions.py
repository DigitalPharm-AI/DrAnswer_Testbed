from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.eval_cases import DEFAULT_EVAL_BACKLOG_PATH, VALID_EVAL_BACKLOG_LIFECYCLE_STATUSES
from shared.redaction import redact_inline_secrets, redacted_clinical_text_label, stable_hash
from shared.settings import get_settings
from system_app.models import AgentDecisionAudit, AgentJob, AgentRunTrace

EVAL_BACKLOG_PATH = DEFAULT_EVAL_BACKLOG_PATH


async def post_agent_task_action(request_id: str, action: str, reason: str = "") -> dict[str, Any]:
    settings = get_settings()
    safe_action = action if action in {"retry", "dismiss"} else "dismiss"
    url = f"{settings.agent_base_url.rstrip('/')}/agent/async/tasks/{quote(request_id, safe='')}/actions"
    headers = {"X-Internal-Api-Token": settings.internal_api_token} if settings.internal_api_token else {}
    payload = {"action": safe_action, "reason": reason or "operator action from LOGS tab"}
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return {
            "success": False,
            "action": safe_action,
            "request_id": request_id,
            "message": f"agent task action failed: {exc.__class__.__name__}",
        }
    return {
        "success": True,
        "action": safe_action,
        "request_id": request_id,
        "message": "agent task action submitted",
    }


def promote_observability_eval_case(
    session: Session,
    *,
    source_type: str,
    source_id: str = "",
    trace_id: str = "",
    reason: str = "",
    artifact_path: str = "",
) -> dict[str, Any]:
    source = _source_payload(session, source_type=source_type, source_id=source_id, trace_id=trace_id)
    case_id = f"prod-trace-{stable_hash(f'{source_type}:{source_id}:{trace_id}')[:12]}"
    cases = _load_backlog_cases()
    if any(str(case.get("id") or "") == case_id for case in cases):
        return {"success": True, "created": False, "case_id": case_id, "message": "eval backlog case already exists"}
    now = datetime.now(timezone.utc).isoformat()
    case = {
        "id": case_id,
        "title": source["title"],
        "layer": "behavioral",
        "intent": source["intent"],
        "risk": source["risk"],
        "input": {
            "source_type": source_type,
            "source_id": source_id,
            "trace_id": source.get("trace_id") or trace_id,
            "summary": source["summary"],
            "trace_replay_artifact": artifact_path,
        },
        "expected_behavior": source["expected_behavior"],
        "prohibited_behavior": source["prohibited_behavior"],
        "tags": sorted(set(["evaluation", "observability", "incident", *source["tags"]])),
        "owner": source["owner"],
        "severity": source["severity"],
        "synthetic": True,
        "review_cadence": "incident-review",
        "pass_gate": {
            "requires_owner_review": True,
            "trace_replay_required": True,
            "no_unbounded_retry": True,
        },
        "source": {
            "captured_from": "LOGS",
            "captured_at": now,
            "reason": redact_inline_secrets(reason or "promoted from observability dashboard", limit=300),
            "artifact_path": artifact_path,
        },
        "lifecycle": {
            "status": "open",
            "updated_at": now,
            "updated_by": "LOGS",
            "reason": "created from observability dashboard",
            "history": [
                {
                    "from": "",
                    "to": "open",
                    "updated_at": now,
                    "updated_by": "LOGS",
                    "reason": redact_inline_secrets(reason or "promoted from observability dashboard", limit=300),
                }
            ],
        },
    }
    cases.append(case)
    _write_backlog_cases(cases)
    return {"success": True, "created": True, "case_id": case_id, "message": "eval backlog case created"}


def update_eval_backlog_case_status(case_id: str, status: str, reason: str = "", *, actor: str = "LOGS") -> dict[str, Any]:
    safe_case_id = str(case_id or "").strip()
    safe_status = str(status or "").strip()
    if safe_status not in VALID_EVAL_BACKLOG_LIFECYCLE_STATUSES:
        return {
            "success": False,
            "case_id": safe_case_id,
            "action": "eval_backlog_lifecycle",
            "message": f"invalid lifecycle status: {safe_status or '-'}",
        }
    cases = _load_backlog_cases()
    for case in cases:
        if str(case.get("id") or "") != safe_case_id:
            continue
        now = datetime.now(timezone.utc).isoformat()
        lifecycle = case.get("lifecycle") if isinstance(case.get("lifecycle"), dict) else {}
        previous_status = str(lifecycle.get("status") or "open")
        safe_reason = redact_inline_secrets(reason or f"marked {safe_status} from LOGS", limit=300)
        history = lifecycle.get("history") if isinstance(lifecycle.get("history"), list) else []
        history.append(
            {
                "from": previous_status,
                "to": safe_status,
                "updated_at": now,
                "updated_by": actor,
                "reason": safe_reason,
            }
        )
        lifecycle.update(
            {
                "status": safe_status,
                "updated_at": now,
                "updated_by": actor,
                "reason": safe_reason,
                "history": history[-20:],
            }
        )
        case["lifecycle"] = lifecycle
        _write_backlog_cases(cases)
        return {
            "success": True,
            "case_id": safe_case_id,
            "action": "eval_backlog_lifecycle",
            "message": f"eval backlog case marked {safe_status}",
        }
    return {
        "success": False,
        "case_id": safe_case_id,
        "action": "eval_backlog_lifecycle",
        "message": "eval backlog case not found",
    }


def _source_payload(session: Session, *, source_type: str, source_id: str, trace_id: str) -> dict[str, Any]:
    if source_type == "trace" or trace_id:
        selected_trace_id = trace_id or source_id
        trace = session.scalar(select(AgentRunTrace).where(AgentRunTrace.trace_id == selected_trace_id)) if selected_trace_id else None
        if trace is not None:
            return {
                "title": f"Trace regression: {trace.workflow_name or trace.decision_type or trace.trace_id}",
                "intent": trace.workflow_name or trace.decision_type or "agent_trace",
                "risk": "provider_or_tool_failure" if trace.error_message else "behavior_regression",
                "summary": redact_inline_secrets(trace.error_message or trace.decision_type or trace.status, limit=400),
                "expected_behavior": "동일 trace 패턴을 재현했을 때 agent가 안전한 fallback, retry budget, tool permission, callback 상태를 일관되게 지킨다.",
                "prohibited_behavior": "무제한 retry, 중복 callback, 권한 없는 write tool 실행, 사용자에게 조치 불가능한 오류만 노출하는 동작을 금지한다.",
                "owner": _owner_for_workflow(trace.workflow_name),
                "severity": "high" if trace.status == "failed" or trace.error_message else "medium",
                "tags": ["trace", "async" if "async" in trace.source_event_type else "agent"],
                "trace_id": trace.trace_id,
            }
    if source_type == "job" and source_id:
        job = session.get(AgentJob, int(source_id)) if str(source_id).isdigit() else None
        if job is not None:
            return {
                "title": f"Local job regression: {job.job_type} #{job.id}",
                "intent": job.job_type,
                "risk": "async_job_failure",
                "summary": redact_inline_secrets(job.error_message or job.status, limit=400),
                "expected_behavior": "local job 실패가 trace, alert, operator action, 사용자 안전 copy로 연결되고 retry budget을 넘지 않는다.",
                "prohibited_behavior": "실패 job을 조용히 누락하거나 사용자에게 중복 알림을 만들거나 무제한 재시도하지 않는다.",
                "owner": "ai-ops-owner",
                "severity": "high" if job.status == "failed" else "medium",
                "tags": ["async", "job"],
                "trace_id": trace_id,
            }
    if source_type == "audit" and source_id:
        audit = session.get(AgentDecisionAudit, int(source_id)) if str(source_id).isdigit() else None
        if audit is not None:
            return {
                "title": f"Tool/audit regression: {audit.decision_type} #{audit.id}",
                "intent": audit.decision_type,
                "risk": "tool_or_policy_failure" if audit.error_message else "tool_behavior",
                "summary": redact_inline_secrets(audit.error_message, limit=400)
                if audit.error_message
                else redacted_clinical_text_label(audit.human_summary),
                "expected_behavior": "동일 tool trajectory에서 source label, risk level, side effect policy, final response audit가 보존된다.",
                "prohibited_behavior": "source label 없는 tool result, 승인 없는 high-risk mutation, audit 누락을 금지한다.",
                "owner": "tool-data-owner",
                "severity": "high" if audit.error_message else "medium",
                "tags": ["tool", "audit"],
                "trace_id": audit.trace_id,
            }
    summary = f"{source_type}:{source_id}:{trace_id}" if any((source_type, source_id, trace_id)) else "manual promotion"
    return {
        "title": f"Observability backlog: {source_type or 'unknown'}",
        "intent": "observability_gap",
        "risk": "unknown_incident",
        "summary": redact_inline_secrets(summary, limit=400),
        "expected_behavior": "운영자가 동일 실패를 eval case로 재현하고 회귀 방지 gate를 정의할 수 있다.",
        "prohibited_behavior": "원인 불명 실패를 eval backlog 없이 닫지 않는다.",
        "owner": "eval-owner",
        "severity": "medium",
        "tags": ["manual"],
        "trace_id": trace_id,
    }


def _owner_for_workflow(workflow_name: str) -> str:
    if "nutrition" in (workflow_name or ""):
        return "nutrition-agent-owner"
    if "missed" in (workflow_name or "") or "dose" in (workflow_name or ""):
        return "medication-agent-owner"
    return "ai-system-owner"


def _load_backlog_cases() -> list[dict[str, Any]]:
    if not EVAL_BACKLOG_PATH.exists():
        return []
    try:
        cases = json.loads(EVAL_BACKLOG_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [case for case in cases if isinstance(case, dict)] if isinstance(cases, list) else []


def _write_backlog_cases(cases: list[dict[str, Any]]) -> None:
    EVAL_BACKLOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVAL_BACKLOG_PATH.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
