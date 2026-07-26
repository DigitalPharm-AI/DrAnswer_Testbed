from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.tool_permissions import requires_human_handoff
from shared.readiness_budget import CostBudget, evaluate_cost_budget
from shared.redaction import redact_inline_secrets, stable_hash
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import AgentRunStep, AgentRunTrace

ONLINE_EVAL_FINDINGS_PATH = Path("data/evals/agent_online_eval_findings.json")
MODEL_FALLBACK_DRILL_PATH = Path("data/governance/model_fallback_drills.json")


def build_readiness_controls_summary(session: Session) -> dict[str, Any]:
    online_eval = build_online_eval_summary(session)
    return {
        "online_eval": online_eval,
        "cost_budget": build_cost_budget_gate(session),
        "model_fallback": build_model_fallback_summary(),
    }


def build_online_eval_summary(session: Session, *, limit: int = 30) -> dict[str, Any]:
    traces = session.scalars(select(AgentRunTrace).order_by(desc(AgentRunTrace.updated_at), desc(AgentRunTrace.id)).limit(limit)).all()
    steps_by_trace = _steps_by_trace(session, [trace.trace_id for trace in traces])
    findings = [_online_eval_trace_finding(trace, steps_by_trace.get(trace.trace_id, [])) for trace in traces]
    status_counts = Counter(finding["status"] for finding in findings)
    failed_count = status_counts.get("fail", 0)
    watch_count = status_counts.get("watch", 0)
    return {
        "rule_id": "health-agent-online-eval-loop-v1",
        "artifact_path": str(ONLINE_EVAL_FINDINGS_PATH),
        "status": "fail" if failed_count else "watch" if watch_count else "pass",
        "trace_count": len(traces),
        "evaluated_count": len(findings),
        "status_counts": [{"label": key, "value": value} for key, value in sorted(status_counts.items())],
        "recent": findings[:6],
        "last_scan": _last_online_eval_scan(),
    }


def record_online_eval_scan(session: Session) -> dict[str, Any]:
    summary = build_online_eval_summary(session)
    payload = {
        "scan_id": f"online-eval-{stable_hash(datetime.now(timezone.utc).isoformat())[:12]}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "status": summary["status"],
            "trace_count": summary["trace_count"],
            "evaluated_count": summary["evaluated_count"],
            "status_counts": summary["status_counts"],
        },
        "findings": summary["recent"],
    }
    scans = _load_json_list(ONLINE_EVAL_FINDINGS_PATH)
    scans.append(payload)
    _write_json_list(ONLINE_EVAL_FINDINGS_PATH, scans[-100:])
    return {
        "success": True,
        "action": "online_eval_scan",
        "artifact_path": str(ONLINE_EVAL_FINDINGS_PATH),
        "message": f"online eval scan recorded: {summary['status']}",
    }


def build_cost_budget_gate(session: Session) -> dict[str, Any]:
    settings = get_settings()
    traces = session.scalars(select(AgentRunTrace)).all()
    today = utc_now().date()
    today_traces = [trace for trace in traces if trace.created_at and trace.created_at.date() == today]
    average_input_tokens = _average_int([trace.input_tokens for trace in traces])
    average_output_tokens = _average_int([trace.output_tokens for trace in traces])
    expected_runs_per_day = max(1, len(today_traces) or len(traces))
    budget = CostBudget(
        daily_budget_usd=settings.agent_daily_cost_budget_usd,
        eval_budget_usd=settings.agent_eval_cost_budget_usd,
        input_usd_per_1m_tokens=settings.agent_cost_input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=settings.agent_cost_output_usd_per_1m_tokens,
    )
    projection = evaluate_cost_budget(
        expected_runs_per_day=expected_runs_per_day,
        average_input_tokens=average_input_tokens,
        average_output_tokens=average_output_tokens,
        budget=budget,
    )
    today_cost_usd = round(sum(max(0.0, float(trace.estimated_cost_usd or 0.0)) for trace in today_traces), 6)
    alerts = list(projection["alerts"])
    if budget.daily_budget_usd > 0 and today_cost_usd > budget.daily_budget_usd:
        alerts.append(
            {
                "code": "observed_daily_cost_budget_exceeded",
                "severity": "critical",
                "actual_usd": today_cost_usd,
                "threshold_usd": budget.daily_budget_usd,
            }
        )
    status = "critical" if any(alert.get("severity") == "critical" for alert in alerts) else "degraded" if alerts else "ok"
    return {
        "rule_id": "health-agent-cost-budget-gate-v1",
        "status": status,
        "today_trace_count": len(today_traces),
        "trace_count": len(traces),
        "expected_runs_per_day": expected_runs_per_day,
        "average_input_tokens": average_input_tokens,
        "average_output_tokens": average_output_tokens,
        "per_run_usd": projection["per_run_usd"],
        "projected_daily_usd": projection["daily_estimate_usd"],
        "observed_today_usd": today_cost_usd,
        "daily_budget_usd": budget.daily_budget_usd,
        "eval_budget_usd": budget.eval_budget_usd,
        "alerts": alerts,
    }


def build_model_fallback_summary() -> dict[str, Any]:
    settings = get_settings()
    drills = _load_json_list(MODEL_FALLBACK_DRILL_PATH)
    drill_mode_counts = Counter(str(item.get("drill_mode") or "rule_based") for item in drills)
    provider = settings.llm_provider.strip().lower()
    return {
        "rule_id": "health-agent-model-fallback-drill-v1",
        "provider": settings.llm_provider,
        "current_model_tier": settings.llm_model_tier,
        "current_model_id": settings.model_id_for_tier(settings.llm_model_tier),
        "fallback_provider": "rule_based",
        "fallback_ready": True,
        "status": "active" if provider in {"rule_based", "rule-based", "local", "heuristic"} else "ready",
        "drill_count": len(drills),
        "drill_mode_counts": [{"label": key, "value": value} for key, value in sorted(drill_mode_counts.items())],
        "last_drill": _last_item_view(drills),
        "runbook": "docs/PRODUCTION_READINESS.md#incident-runbook",
    }


async def record_model_fallback_drill(drill_mode: str = "rule_based") -> dict[str, Any]:
    settings = get_settings()
    model_config = await _fetch_agent_model_config(settings)
    safe_drill_mode = drill_mode if drill_mode in {"rule_based", "testbed"} else "rule_based"
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "drill_id": f"fallback-{stable_hash(now)[:12]}",
        "drill_mode": safe_drill_mode,
        "created_at": now,
        "provider": settings.llm_provider,
        "fallback_provider": "rule_based",
        "agent_model_config": model_config,
        "containment": {
            "action": _fallback_drill_action(safe_drill_mode),
            "rollback": "restore previous LLM_PROVIDER and model tier after evals and canary pass",
            "owner": "ai-ops-owner",
        },
        "status": "recorded",
    }
    drills = _load_json_list(MODEL_FALLBACK_DRILL_PATH)
    drills.append(payload)
    _write_json_list(MODEL_FALLBACK_DRILL_PATH, drills[-50:])
    return {
        "success": True,
        "action": "model_fallback_drill",
        "drill_mode": safe_drill_mode,
        "artifact_path": str(MODEL_FALLBACK_DRILL_PATH),
        "message": f"{safe_drill_mode} model fallback drill recorded",
    }


def _fallback_drill_action(drill_mode: str) -> str:
    if drill_mode == "testbed":
        return "record testbed fallback evidence without changing live traffic"
    return "set LLM_PROVIDER=rule_based and restart agent_app/worker"


def _online_eval_trace_finding(trace: AgentRunTrace, steps: list[AgentRunStep]) -> dict[str, Any]:
    reasons: list[str] = []
    status = "pass"
    if trace.status == "failed" or trace.error_message:
        status = "fail"
        reasons.append("trace failed or has error_message")
    tool_error_count = sum(1 for step in steps if step.step_type == "tool_call" and step.status in {"error", "failed"})
    if tool_error_count:
        status = "fail"
        reasons.append(f"tool errors={tool_error_count}")
    unsafe_high_risk = [
        step.tool_name
        for step in steps
        if requires_human_handoff(step.tool_name) and step.status not in {"skipped", "error"}
    ]
    if unsafe_high_risk:
        status = "fail"
        reasons.append(f"high-risk handoff missing={','.join(sorted(set(unsafe_high_risk)))}")
    if status == "pass" and trace.latency_ms and trace.latency_ms > 5000:
        status = "watch"
        reasons.append("trace latency over 5000ms")
    if status == "pass" and trace.estimated_cost_usd and trace.estimated_cost_usd > 1:
        status = "watch"
        reasons.append("trace cost over $1")
    return {
        "trace_id": trace.trace_id,
        "short_trace_id": _short_id(trace.trace_id),
        "workflow_name": trace.workflow_name or trace.decision_type or "-",
        "agent_name": trace.agent_name or "-",
        "status": status,
        "reason": "; ".join(reasons) if reasons else "deterministic online checks passed",
        "owner": _owner_for_trace(trace),
        "updated_at": trace.updated_at.isoformat() if trace.updated_at else "",
    }


def _steps_by_trace(session: Session, trace_ids: list[str]) -> dict[str, list[AgentRunStep]]:
    if not trace_ids:
        return {}
    rows = session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id.in_(trace_ids)).order_by(AgentRunStep.id.asc())).all()
    grouped: dict[str, list[AgentRunStep]] = {}
    for row in rows:
        grouped.setdefault(row.trace_id, []).append(row)
    return grouped


async def _fetch_agent_model_config(settings) -> dict[str, Any]:
    url = f"{settings.agent_base_url.rstrip('/')}/agent/model-config"
    headers = {"X-Internal-Api-Token": settings.internal_api_token} if settings.internal_api_token else {}
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "available": False,
            "error": redact_inline_secrets(f"{exc.__class__.__name__}: {exc}", limit=180),
        }
    return payload if isinstance(payload, dict) else {"available": False, "error": "invalid_model_config_payload"}


def _last_online_eval_scan() -> dict[str, Any]:
    return _last_item_view(_load_json_list(ONLINE_EVAL_FINDINGS_PATH))


def _last_item_view(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {}
    item = items[-1]
    return {
        "id": item.get("scan_id") or item.get("drill_id") or "-",
        "created_at": item.get("created_at") or "",
        "status": item.get("status") or item.get("summary", {}).get("status") or "recorded",
        "drill_mode": item.get("drill_mode") or "",
    }


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _write_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _average_int(values: list[int]) -> int:
    clean_values = [max(0, int(value or 0)) for value in values]
    if not clean_values:
        return 0
    return int(round(sum(clean_values) / len(clean_values)))


def _owner_for_trace(trace: AgentRunTrace) -> str:
    workflow = (trace.workflow_name or trace.decision_type or "").lower()
    if "nutrition" in workflow:
        return "nutrition-agent-owner"
    if any(token in workflow for token in ("dose", "medication", "missed")):
        return "medication-agent-owner"
    return "ai-system-owner"


def _short_id(value: str) -> str:
    if len(value) <= 16:
        return value
    return f"{value[:8]}...{value[-6:]}"
