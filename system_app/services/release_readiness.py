from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

RELEASE_READINESS_OUTPUT_DIR = Path("outputs/readiness")
EVAL_REPORTS_DIR = Path("outputs/evals")


def build_release_readiness_preview(observability: dict[str, Any]) -> dict[str, Any]:
    return {
        "rule_id": "health-agent-release-readiness-v1",
        "scorecard": build_readiness_scorecard(observability),
        "latest_eval": latest_eval_report(),
        "latest_report": latest_release_report(),
        "output_dir": str(RELEASE_READINESS_OUTPUT_DIR),
    }


def build_release_readiness_report(observability: dict[str, Any]) -> dict[str, Any]:
    preview = build_release_readiness_preview(observability)
    trace_dashboard = _dict_at(observability, "trace_dashboard")
    alerts = _dict_at(observability, "agent_alerts")
    local_jobs = _dict_at(observability, "local_jobs")
    controls = _dict_at(observability, "readiness_controls")
    eval_backlog = _dict_at(observability, "eval_backlog")
    return {
        "artifact_type": "release_readiness_report",
        "artifact_version": "v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "decision": preview["scorecard"]["status"],
        "decision_reason": _decision_reason(preview["scorecard"]),
        "scorecard": preview["scorecard"],
        "latest_eval": preview["latest_eval"],
        "alerts": {
            "status": alerts.get("status") or "unknown",
            "count": _int_value(alerts.get("count")),
            "items": _alert_rows(alerts.get("items")),
        },
        "failed_jobs": _failed_job_rows(local_jobs.get("items")),
        "cost_budget": _dict_at(controls, "cost_budget"),
        "model_fallback": _dict_at(controls, "model_fallback"),
        "trace_coverage": _trace_coverage(trace_dashboard),
        "eval_backlog": {
            "path": eval_backlog.get("path") or "",
            "dataset_path": eval_backlog.get("dataset_path") or "",
            "count": _int_value(eval_backlog.get("count")),
            "included_count": _int_value(eval_backlog.get("included_count")),
            "blocking_count": _int_value(eval_backlog.get("blocking_count")),
            "lifecycle_counts": eval_backlog.get("lifecycle_counts") if isinstance(eval_backlog.get("lifecycle_counts"), dict) else {},
        },
    }


def write_release_readiness_report(observability: dict[str, Any]) -> dict[str, Any]:
    report = build_release_readiness_report(observability)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = RELEASE_READINESS_OUTPUT_DIR / f"release-readiness-{stamp}.json"
    markdown_path = RELEASE_READINESS_OUTPUT_DIR / f"release-readiness-{stamp}.md"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(_render_markdown_report(report), encoding="utf-8")
    return {
        "success": True,
        "action": "release_readiness_report",
        "artifact_path": str(json_path),
        "markdown_path": str(markdown_path),
        "message": f"release readiness report written: {report['decision']}",
    }


def build_readiness_scorecard(observability: dict[str, Any]) -> dict[str, Any]:
    gates = _readiness_gates(observability)
    max_points = sum(gate["max_points"] for gate in gates)
    points = sum(gate["points"] for gate in gates)
    score = int(round((points / max_points) * 100)) if max_points else 0
    failed = [gate for gate in gates if gate["status"] == "fail"]
    blocking = [gate for gate in failed if gate["blocking"]]
    watch = [gate for gate in gates if gate["status"] == "watch"]
    status = "blocked" if blocking else "needs_review" if failed or watch else "ready"
    return {
        "score": score,
        "status": status,
        "gate_count": len(gates),
        "pass_count": sum(1 for gate in gates if gate["status"] == "pass"),
        "watch_count": len(watch),
        "fail_count": len(failed),
        "blocking_count": len(blocking),
        "gates": gates,
    }


def latest_eval_report() -> dict[str, Any]:
    return _latest_json_artifact(EVAL_REPORTS_DIR, "*.json", "eval_report")


def latest_release_report() -> dict[str, Any]:
    return _latest_json_artifact(RELEASE_READINESS_OUTPUT_DIR, "release-readiness-*.json", "release_readiness_report")


def _readiness_gates(observability: dict[str, Any]) -> list[dict[str, Any]]:
    controls = _dict_at(observability, "readiness_controls")
    online_eval = _dict_at(controls, "online_eval")
    cost_budget = _dict_at(controls, "cost_budget")
    model_fallback = _dict_at(controls, "model_fallback")
    trace_dashboard = _dict_at(observability, "trace_dashboard")
    alerts = _dict_at(observability, "agent_alerts")
    eval_backlog = _dict_at(observability, "eval_backlog")
    local_jobs = _dict_at(observability, "local_jobs")

    alert_items = alerts.get("items") if isinstance(alerts.get("items"), list) else []
    has_critical_alert = any(str(item.get("severity") or "").lower() == "critical" for item in alert_items if isinstance(item, dict))
    alert_count = _int_value(alerts.get("count"))
    failed_trace_count = _int_value(trace_dashboard.get("failed_trace_count"))
    failed_jobs = [row for row in local_jobs.get("items", []) if isinstance(row, dict) and row.get("status") == "failed"]
    fallback_ready = bool(model_fallback.get("fallback_ready"))
    fallback_drill_count = _int_value(model_fallback.get("drill_count"))
    blocking_backlog_count = _int_value(eval_backlog.get("blocking_count"))

    gates = [
        _gate(
            gate_id="online_eval",
            label="Online eval deterministic scan",
            pillar="Evaluation",
            raw_status=str(online_eval.get("status") or "watch"),
            evidence=f"{online_eval.get('evaluated_count', 0)} traces evaluated",
            target="status pass",
            owner="eval-owner",
            next_action="Record scan, inspect fail/watch findings, then promote incidents to backlog.",
            max_points=20,
        ),
        _gate(
            gate_id="failed_traces",
            label="Failed trace backlog",
            pillar="Observability",
            raw_status="fail" if failed_trace_count else "pass",
            evidence=f"{failed_trace_count} failed traces",
            target="0 failed traces before release",
            owner="ai-ops-owner",
            next_action="Write replay artifacts and promote failed traces to eval backlog.",
            max_points=15,
        ),
        _gate(
            gate_id="cost_budget",
            label="Cost budget gate",
            pillar="Governance",
            raw_status=str(cost_budget.get("status") or "degraded"),
            evidence=f"projected daily ${cost_budget.get('projected_daily_usd', 0)} / budget ${cost_budget.get('daily_budget_usd', 0)}",
            target="status ok",
            owner="ops-product-owner",
            next_action="Configure token prices and keep projected/observed cost under budget.",
            max_points=15,
        ),
        _gate(
            gate_id="model_fallback",
            label="Model fallback drill",
            pillar="Governance",
            raw_status="pass" if fallback_ready and fallback_drill_count > 0 else "watch" if fallback_ready else "fail",
            evidence=f"fallback_ready={fallback_ready}, drills={fallback_drill_count}",
            target="fallback ready with at least one recorded drill",
            owner="ai-ops-owner",
            next_action="Record fallback drill and verify rule_based containment path.",
            max_points=15,
            blocking=False,
        ),
        _gate(
            gate_id="eval_backlog",
            label="Eval backlog release blockers",
            pillar="Evaluation",
            raw_status="fail" if blocking_backlog_count else "pass",
            evidence=f"{blocking_backlog_count} CI blocking backlog cases",
            target="0 blocking high/critical backlog cases",
            owner="eval-owner",
            next_action="Review, attach regression evidence, then mark cleared only after gate passes.",
            max_points=15,
        ),
        _gate(
            gate_id="agent_alerts",
            label="Agent alerts",
            pillar="Observability",
            raw_status="fail" if has_critical_alert else "watch" if alert_count else "pass",
            evidence=f"{alert_count} active alerts",
            target="0 critical alerts",
            owner="ai-system-owner",
            next_action="Triage alert owners and containment runbooks before release.",
            max_points=10,
        ),
        _gate(
            gate_id="local_jobs",
            label="Local job failures",
            pillar="Orchestration",
            raw_status="fail" if failed_jobs else "pass",
            evidence=f"{len(failed_jobs)} failed local jobs",
            target="0 failed local jobs",
            owner="system-app-owner",
            next_action="Retry or dismiss failed jobs with incident evidence.",
            max_points=10,
        ),
    ]
    return gates


def _gate(
    *,
    gate_id: str,
    label: str,
    pillar: str,
    raw_status: str,
    evidence: str,
    target: str,
    owner: str,
    next_action: str,
    max_points: int,
    blocking: bool = True,
) -> dict[str, Any]:
    status = _normalize_gate_status(raw_status)
    points = max_points if status == "pass" else int(round(max_points * 0.5)) if status == "watch" else 0
    return {
        "id": gate_id,
        "label": label,
        "pillar": pillar,
        "status": status,
        "points": points,
        "max_points": max_points,
        "blocking": blocking,
        "evidence": evidence,
        "target": target,
        "owner": owner,
        "next_action": next_action,
    }


def _normalize_gate_status(value: str) -> str:
    lowered = str(value or "").strip().lower()
    if lowered in {"ok", "pass", "passed", "ready", "active"}:
        return "pass"
    if lowered in {"watch", "degraded", "needs_review", "warning", "review"}:
        return "watch"
    return "fail" if lowered in {"fail", "failed", "critical", "blocked", "error"} else "watch"


def _latest_json_artifact(directory: Path, pattern: str, artifact_type: str) -> dict[str, Any]:
    paths = sorted(directory.glob(pattern), key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    if not paths:
        return {"available": False, "artifact_type": artifact_type, "path": str(directory), "status": "missing"}
    path = paths[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {
            "available": False,
            "artifact_type": artifact_type,
            "path": str(path),
            "status": "unreadable",
            "error": f"{exc.__class__.__name__}: {exc}",
        }
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    scorecard = payload.get("scorecard") if isinstance(payload.get("scorecard"), dict) else {}
    return {
        "available": True,
        "artifact_type": artifact_type,
        "path": str(path),
        "status": payload.get("status") or payload.get("decision") or scorecard.get("status") or "recorded",
        "generated_at": payload.get("generated_at") or "",
        "summary": summary,
        "case_count": payload.get("case_count"),
        "selected_count": payload.get("selected_count"),
        "score": scorecard.get("score"),
    }


def _trace_coverage(trace_dashboard: dict[str, Any]) -> dict[str, Any]:
    step_counts = trace_dashboard.get("step_counts") if isinstance(trace_dashboard.get("step_counts"), list) else []
    observed_steps = {str(row.get("label") or ""): _int_value(row.get("value")) for row in step_counts if isinstance(row, dict)}
    required = ["workflow/run", "model_call", "tool_call", "final_response"]
    missing = [step for step in required if observed_steps.get(step, 0) <= 0]
    return {
        "trace_count": _int_value(trace_dashboard.get("trace_count")),
        "event_count": _int_value(trace_dashboard.get("event_count")),
        "failed_trace_count": _int_value(trace_dashboard.get("failed_trace_count")),
        "tool_call_count": _int_value(trace_dashboard.get("tool_call_count")),
        "model_error_count": _int_value(trace_dashboard.get("model_error_count")),
        "tool_error_count": _int_value(trace_dashboard.get("tool_error_count")),
        "required_step_types": required,
        "observed_step_counts": observed_steps,
        "missing_step_types": missing,
        "status": "needs_review" if missing else "ok",
    }


def _alert_rows(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [
        {
            "code": row.get("code") or "agent_alert",
            "severity": row.get("severity") or "warning",
            "owner": row.get("owner") or "ai-system-owner",
            "action_status": row.get("action_status") or "open",
            "runbook": row.get("runbook") or "",
        }
        for row in items
        if isinstance(row, dict)
    ]


def _failed_job_rows(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or item.get("status") != "failed":
            continue
        rows.append(
            {
                "id": item.get("id"),
                "job_type": item.get("job_type"),
                "root_cause": item.get("root_cause"),
                "updated_label": item.get("updated_label"),
                "error_message": item.get("error_message"),
            }
        )
    return rows


def _decision_reason(scorecard: dict[str, Any]) -> str:
    if scorecard["status"] == "ready":
        return "All release readiness gates passed."
    if scorecard["blocking_count"]:
        return f"{scorecard['blocking_count']} blocking readiness gates failed."
    return f"{scorecard['watch_count']} gates require operator review."


def _render_markdown_report(report: dict[str, Any]) -> str:
    scorecard = report["scorecard"]
    lines = [
        "# Release Readiness Report",
        "",
        f"Generated: {report['generated_at']}",
        f"Decision: {report['decision']}",
        f"Score: {scorecard['score']}/100",
        f"Reason: {report['decision_reason']}",
        "",
        "## Gates",
        "",
        "| Gate | Pillar | Status | Evidence | Owner | Next action |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for gate in scorecard["gates"]:
        lines.append(
            f"| {gate['label']} | {gate['pillar']} | {gate['status']} | {gate['evidence']} | {gate['owner']} | {gate['next_action']} |"
        )
    latest_eval = report["latest_eval"]
    lines.extend(
        [
            "",
            "## Latest Eval",
            "",
            f"- Status: {latest_eval.get('status')}",
            f"- Path: {latest_eval.get('path')}",
            f"- Summary: {json.dumps(latest_eval.get('summary') or {}, ensure_ascii=False)}",
            "",
            "## Trace Coverage",
            "",
            f"- Status: {report['trace_coverage']['status']}",
            f"- Trace count: {report['trace_coverage']['trace_count']}",
            f"- Event count: {report['trace_coverage']['event_count']}",
            f"- Missing step types: {', '.join(report['trace_coverage']['missing_step_types']) or 'none'}",
            "",
            "## Cost And Fallback",
            "",
            f"- Cost status: {report['cost_budget'].get('status')}",
            f"- Projected daily cost: ${report['cost_budget'].get('projected_daily_usd')}",
            f"- Fallback status: {report['model_fallback'].get('status')}",
            f"- Fallback drills: {report['model_fallback'].get('drill_count')}",
        ]
    )
    return "\n".join(lines) + "\n"


def _dict_at(value: dict[str, Any], key: str) -> dict[str, Any]:
    item = value.get(key) if isinstance(value, dict) else {}
    return item if isinstance(item, dict) else {}


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0
