from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from agent_app.tools.catalog import ToolCatalog
from agent_app.tools.names import (
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
from agent_app.tools.permissions import requires_human_handoff
from shared.json_utils import parse_json_object
from shared.eval_cases import (
    DEFAULT_EVAL_BACKLOG_PATH,
    DEFAULT_EVAL_DATASET_PATH,
    eval_case_id,
    eval_case_lifecycle_status,
    eval_case_schema_findings,
    is_blocking_backlog_case,
    load_eval_case_file,
)
from shared.redaction import redact_inline_secrets, redacted_clinical_text_label
from shared.settings import get_settings
from shared.time_utils import utc_now
from system_app.models import AgentDecisionAudit, AgentJob, AgentRunStep, AgentRunTrace, Notification
from system_app.services.clock_service import ensure_clock
from system_app.services.governance_change_log import CHANGE_LOG_PATH, governance_change_log_summary
from system_app.services.readiness_controls import build_readiness_controls_summary
from system_app.services.release_readiness import build_release_readiness_preview
from system_app.services.trace_retention import TRACE_RETENTION_POLICY, trace_retention_expired_count

AGENT_TIMEOUT_SECONDS = 0.8
EVAL_BACKLOG_PATH = DEFAULT_EVAL_BACKLOG_PATH
EVAL_DATASET_PATH = DEFAULT_EVAL_DATASET_PATH
REPO_ROOT = Path(__file__).resolve().parents[2]


async def build_observability_context(session: Session) -> dict[str, Any]:
    settings = get_settings()
    agent_status, agent_ops, agent_tasks, agent_model_config = await _fetch_agent_observability(settings)
    local_job_counts = _local_job_counts(session)
    trace_summary = _trace_dashboard(session)
    tool_catalog = _tool_catalog()
    tool_logs = _tool_execution_logs(session)
    alerts = _agent_alerts(agent_ops, trace_summary, local_job_counts)
    agent_task_view = _agent_tasks(agent_tasks)
    readiness_controls = build_readiness_controls_summary(session)
    eval_backlog = _eval_backlog_summary()
    change_log = governance_change_log_summary()
    async_status = _async_status(settings, local_job_counts, agent_status, agent_ops)
    workers = _workers(agent_status, agent_ops)
    local_jobs = _recent_local_jobs(session)
    context = {
        "async_status": async_status,
        "workers": workers,
        "local_jobs": local_jobs,
        "agent_alerts": alerts,
        "trace_dashboard": trace_summary,
        "quality_metrics": _online_quality_metrics(trace_summary, agent_task_view),
        "readiness_controls": readiness_controls,
        "eval_backlog": eval_backlog,
        "change_log": change_log,
        "tool_catalog": tool_catalog,
        "tool_logs": tool_logs,
        "agent_tasks": agent_task_view,
    }
    context["release_readiness"] = build_release_readiness_preview(context)
    context["prompt_model_ops"] = _prompt_model_ops(settings, agent_model_config, readiness_controls, change_log)
    context["prompt_model_approval"] = _prompt_model_approval(change_log)
    context["business_metric_gate"] = _business_metric_gate(context)
    context["data_quality_drift"] = _data_quality_drift(tool_catalog, eval_backlog, change_log)
    context["containment_action"] = _containment_action(async_status, alerts, agent_task_view, local_jobs, readiness_controls)
    context["agent_conversation_history"] = _agent_conversation_history(session)
    context["incident_timeline"] = _incident_timeline(alerts, trace_summary, local_jobs, eval_backlog, tool_logs, agent_task_view)
    return context


async def _fetch_agent_observability(settings) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    async with httpx.AsyncClient(timeout=AGENT_TIMEOUT_SECONDS, trust_env=False) as client:
        return await _gather_agent_payloads(client, settings)


async def _gather_agent_payloads(client: httpx.AsyncClient, settings) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    status, ops, tasks, model_config = await asyncio.gather(
        _fetch_agent_json(client, settings, "/agent/async/tasks/status"),
        _fetch_agent_json(client, settings, "/agent/ops/readiness"),
        _fetch_agent_json(client, settings, "/agent/async/tasks?limit=20"),
        _fetch_agent_json(client, settings, "/agent/model-config"),
    )
    return status, ops, tasks, model_config


async def _fetch_agent_json(client: httpx.AsyncClient, settings, path: str) -> dict[str, Any]:
    url = f"{settings.agent_base_url.rstrip('/')}{path}"
    headers = {"X-Internal-Api-Token": settings.internal_api_token} if settings.internal_api_token else {}
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return {"ok": False, "url": url, "error": exc.__class__.__name__, "payload": {}}
    payload: dict[str, Any]
    try:
        raw_payload = response.json()
        payload = raw_payload if isinstance(raw_payload, dict) else {}
    except ValueError:
        payload = {}
    return {
        "ok": response.is_success,
        "url": url,
        "status_code": response.status_code,
        "payload": payload,
    }


def _prompt_model_ops(
    settings,
    agent_model_config: dict[str, Any],
    readiness_controls: dict[str, Any],
    change_log: dict[str, Any],
) -> dict[str, Any]:
    payload = _payload(agent_model_config)
    available_tiers = payload.get("available_tiers")
    if not isinstance(available_tiers, dict) or not available_tiers:
        available_tiers = settings.available_model_tiers()
    model_tier = str(payload.get("model_tier") or settings.llm_model_tier)
    provider = str(payload.get("provider") or settings.llm_provider)
    model_id = str(payload.get("model_id") or settings.model_id_for_tier(model_tier))
    tiers = [
        {
            "tier": str(tier),
            "model_id": str(tier_model_id),
            "is_current": str(tier) == model_tier,
        }
        for tier, tier_model_id in sorted(available_tiers.items())
    ]
    fallback = readiness_controls.get("model_fallback") if isinstance(readiness_controls.get("model_fallback"), dict) else {}
    recent_changes = change_log.get("recent") if isinstance(change_log.get("recent"), list) else []
    recent_model_changes = [entry for entry in recent_changes if entry.get("change_type") == "model"]
    rollback_tier = _default_rollback_tier(available_tiers, model_tier)
    return {
        "status": "ok" if agent_model_config.get("ok") else "fallback",
        "provider": provider,
        "model_tier": model_tier,
        "model_id": model_id,
        "available_tiers": tiers,
        "rollback_tier": rollback_tier,
        "agent_base_url": settings.agent_base_url,
        "config_url": agent_model_config.get("url") or f"{settings.agent_base_url.rstrip('/')}/agent/model-config",
        "config_error": agent_model_config.get("error") or "",
        "fallback_ready": bool(fallback.get("fallback_ready")),
        "fallback_status": fallback.get("status") or "unknown",
        "fallback_provider": fallback.get("fallback_provider") or "rule_based",
        "drill_count": _int_value(fallback.get("drill_count")),
        "last_drill": fallback.get("last_drill") if isinstance(fallback.get("last_drill"), dict) else {},
        "recent_model_changes": recent_model_changes[:3],
    }


def _default_rollback_tier(available_tiers: dict[str, Any], current_tier: str) -> str:
    if "fast" in available_tiers and current_tier != "fast":
        return "fast"
    for tier in sorted(str(item) for item in available_tiers):
        if tier != current_tier:
            return tier
    return current_tier


def _prompt_model_approval(change_log: dict[str, Any]) -> dict[str, Any]:
    recent = change_log.get("recent") if isinstance(change_log.get("recent"), list) else []
    status_counts = Counter(str(entry.get("approval_status") or "recorded") for entry in recent)
    return {
        "path": change_log.get("path") or str(CHANGE_LOG_PATH),
        "count": _int_value(change_log.get("count")),
        "approval_counts": [{"label": key, "value": value} for key, value in sorted(status_counts.items())],
        "pending_count": sum(1 for entry in recent if str(entry.get("approval_status") or "recorded") in {"recorded", "pending"}),
        "recent": recent[:6],
    }


def _business_metric_gate(context: dict[str, Any]) -> dict[str, Any]:
    release = context.get("release_readiness") if isinstance(context.get("release_readiness"), dict) else {}
    scorecard = release.get("scorecard") if isinstance(release.get("scorecard"), dict) else {}
    quality = context.get("quality_metrics") if isinstance(context.get("quality_metrics"), dict) else {}
    controls = context.get("readiness_controls") if isinstance(context.get("readiness_controls"), dict) else {}
    cost_budget = controls.get("cost_budget") if isinstance(controls.get("cost_budget"), dict) else {}
    eval_backlog = context.get("eval_backlog") if isinstance(context.get("eval_backlog"), dict) else {}
    gates = scorecard.get("gates") if isinstance(scorecard.get("gates"), list) else []
    blocking_gates = [gate for gate in gates if gate.get("status") == "blocking"]
    latest_eval = release.get("latest_eval") if isinstance(release.get("latest_eval"), dict) else {}
    latest_report = release.get("latest_report") if isinstance(release.get("latest_report"), dict) else {}
    hard_blockers = _business_hard_blockers(blocking_gates, eval_backlog, latest_eval, cost_budget)
    score_status = str(scorecard.get("status") or "unknown")
    if hard_blockers:
        release_gate_status = "release_blocked"
        release_decision = "Release blocked: hard blockers require owner review."
    elif score_status == "ready":
        release_gate_status = "release_ready"
        release_decision = "Release ready: no hard blockers detected."
    else:
        release_gate_status = "release_watch"
        release_decision = "Release watch: verify quality, cost, and eval evidence before rollout."
    return {
        "rule_id": "health-agent-business-metric-gate-v1",
        "status": score_status,
        "release_gate_status": release_gate_status,
        "release_decision": release_decision,
        "score": _int_value(scorecard.get("score")),
        "pass_count": _int_value(scorecard.get("pass_count")),
        "watch_count": _int_value(scorecard.get("watch_count")),
        "blocking_count": _int_value(scorecard.get("blocking_count")) + _int_value(eval_backlog.get("blocking_count")),
        "hard_blocker_count": len(hard_blockers),
        "hard_blockers": hard_blockers[:6],
        "quality_metrics": quality.get("metrics") if isinstance(quality.get("metrics"), list) else [],
        "quality_gates": quality.get("gates") if isinstance(quality.get("gates"), list) else [],
        "cost_budget": cost_budget,
        "eval_backlog": eval_backlog,
        "blocking_gates": blocking_gates[:5],
        "latest_eval": latest_eval,
        "latest_report": latest_report,
    }


def _business_hard_blockers(
    blocking_gates: list[dict[str, Any]],
    eval_backlog: dict[str, Any],
    latest_eval: dict[str, Any],
    cost_budget: dict[str, Any],
) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    for gate in blocking_gates:
        blockers.append(
            {
                "source": "release_scorecard",
                "label": str(gate.get("label") or "release gate"),
                "evidence": str(gate.get("evidence") or gate.get("status") or "blocking"),
            }
        )
    eval_blocking_count = _int_value(eval_backlog.get("blocking_count"))
    if eval_blocking_count:
        blockers.append(
            {
                "source": "eval_backlog",
                "label": "Eval backlog CI gate",
                "evidence": f"{eval_blocking_count} blocking cases require review/regression evidence",
            }
        )
    latest_eval_status = str(latest_eval.get("status") or "").lower()
    if latest_eval_status in {"fail", "failed", "blocked", "error"}:
        blockers.append(
            {
                "source": "latest_eval",
                "label": "Latest eval report",
                "evidence": latest_eval.get("path") or latest_eval_status,
            }
        )
    cost_status = str(cost_budget.get("status") or "").lower()
    if cost_status in {"fail", "failed", "blocked", "over_budget"}:
        blockers.append(
            {
                "source": "cost_budget",
                "label": "Cost budget",
                "evidence": str(cost_budget.get("message") or cost_budget.get("projected_daily_usd") or cost_status),
            }
        )
    daily_budget = _float_value(cost_budget.get("daily_budget_usd") or cost_budget.get("budget_daily_usd"))
    projected_daily = _float_value(cost_budget.get("projected_daily_usd"))
    if daily_budget > 0 and projected_daily > daily_budget and not any(row["source"] == "cost_budget" for row in blockers):
        blockers.append(
            {
                "source": "cost_budget",
                "label": "Cost budget",
                "evidence": f"projected ${projected_daily:.2f} exceeds daily budget ${daily_budget:.2f}",
            }
        )
    return blockers


def _data_quality_drift(
    tool_catalog: dict[str, Any],
    eval_backlog: dict[str, Any],
    change_log: dict[str, Any],
) -> dict[str, Any]:
    data_sources = tool_catalog.get("data_sources") if isinstance(tool_catalog.get("data_sources"), list) else []
    freshness_counts = tool_catalog.get("freshness_counts") if isinstance(tool_catalog.get("freshness_counts"), list) else []
    source_status_counts = Counter(str(source.get("status") or "unknown") for source in data_sources if isinstance(source, dict))
    tool_freshness_counts = {
        str(row.get("label") or "").replace("freshness ", ""): _int_value(row.get("value"))
        for row in freshness_counts
        if isinstance(row, dict)
    }
    missing_count = source_status_counts.get("missing", 0) + tool_freshness_counts.get("missing source", 0)
    needs_review_count = source_status_counts.get("needs_review", 0) + tool_freshness_counts.get("catalog mismatch", 0)
    status = "degraded" if missing_count or needs_review_count or eval_backlog.get("error") else "ok"
    return {
        "rule_id": "health-agent-data-quality-drift-v1",
        "status": status,
        "source_counts": [{"label": key, "value": value} for key, value in sorted(source_status_counts.items())],
        "tool_freshness_counts": freshness_counts,
        "missing_count": missing_count,
        "needs_review_count": needs_review_count,
        "data_sources": data_sources,
        "eval_backlog_path": eval_backlog.get("path") or "",
        "eval_dataset_path": eval_backlog.get("dataset_path") or "",
        "change_log_path": change_log.get("path") or str(CHANGE_LOG_PATH),
        "eval_error": eval_backlog.get("error") or "",
    }


def _containment_action(
    async_status: dict[str, Any],
    alerts: dict[str, Any],
    agent_tasks: dict[str, Any],
    local_jobs: dict[str, Any],
    readiness_controls: dict[str, Any],
) -> dict[str, Any]:
    jobs = local_jobs.get("items") if isinstance(local_jobs.get("items"), list) else []
    failed_jobs = [job for job in jobs if job.get("status") == "failed"]
    model_fallback = readiness_controls.get("model_fallback") if isinstance(readiness_controls.get("model_fallback"), dict) else {}
    blocked = (
        async_status.get("remote_status") != "ok"
        or _int_value(agent_tasks.get("dead_count")) > 0
        or bool(failed_jobs)
        or _int_value(alerts.get("count")) > 0
    )
    return {
        "rule_id": "health-agent-containment-action-v1",
        "status": "needs_action" if blocked else "ready",
        "remote_status": async_status.get("remote_status") or "unknown",
        "dead_count": _int_value(agent_tasks.get("dead_count")),
        "failed_job_count": len(failed_jobs),
        "alert_count": _int_value(alerts.get("count")),
        "fallback_ready": bool(model_fallback.get("fallback_ready")),
        "fallback_provider": model_fallback.get("fallback_provider") or "rule_based",
        "recommended_actions": _containment_recommendations(async_status, alerts, agent_tasks, failed_jobs, model_fallback),
    }


def _containment_recommendations(
    async_status: dict[str, Any],
    alerts: dict[str, Any],
    agent_tasks: dict[str, Any],
    failed_jobs: list[dict[str, Any]],
    model_fallback: dict[str, Any],
) -> list[dict[str, str]]:
    actions: list[dict[str, str]] = []
    if async_status.get("remote_status") != "ok":
        actions.append({"label": "Agent 연결 확인", "detail": async_status.get("agent_error") or async_status.get("agent_base_url") or "-"})
    if _int_value(agent_tasks.get("dead_count")):
        actions.append({"label": "Dead task retry/dismiss", "detail": f"{_int_value(agent_tasks.get('dead_count'))} dead tasks"})
    if failed_jobs:
        actions.append({"label": "Local job eval backlog 등록", "detail": f"{len(failed_jobs)} failed local jobs"})
    if _int_value(alerts.get("count")):
        actions.append({"label": "Alert runbook 점검", "detail": f"{_int_value(alerts.get('count'))} active alerts"})
    if model_fallback.get("fallback_ready"):
        actions.append({"label": "Rule-based fallback 기록", "detail": str(model_fallback.get("runbook") or "docs/PRODUCTION_READINESS.md#incident-runbook")})
    if not actions:
        actions.append({"label": "Containment 대기", "detail": "현재 즉시 조치할 agent incident가 없습니다."})
    return actions


def _agent_conversation_history(session: Session) -> dict[str, Any]:
    clock = ensure_clock(session)
    day_start = datetime.combine(clock.current_time.date(), datetime.min.time())
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.notification_type == "system_policy_request",
            Notification.visible_at >= day_start,
            Notification.visible_at <= clock.current_time,
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(20)
    ).all()
    items = []
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        status = str(metadata.get("status") or "sent")
        items.append(
            {
                "id": row.id,
                "message": metadata.get("request_message") or row.body,
                "result_message": metadata.get("result_message") or "",
                "status": status,
                "status_label": _system_request_status_label(status),
                "trace_id": metadata.get("trace_id") or "",
                "decision_type": metadata.get("decision_type") or "",
                "agent_name": metadata.get("agent_name") or "",
                "time_label": row.visible_at.strftime("%m-%d %H:%M"),
            }
        )
    return {
        "count": len(items),
        "items": items,
    }


def _system_request_status_label(status: str) -> str:
    return {
        "sent": "전송",
        "awaiting_agent": "대기",
        "answered": "응답",
        "applied": "반영",
        "needs_confirmation": "확인 대기",
        "needs_clarification": "추가 확인",
        "failed": "실패",
    }.get(status, status)


def _incident_timeline(
    alerts: dict[str, Any],
    trace_summary: dict[str, Any],
    local_jobs: dict[str, Any],
    eval_backlog: dict[str, Any],
    tool_logs: dict[str, Any],
    agent_tasks: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, str]] = []
    for alert in (alerts.get("items") if isinstance(alerts.get("items"), list) else [])[:4]:
        items.append(
            _incident_item(
                "Detect",
                alert.get("code") or "agent_alert",
                alert.get("message") or alert.get("details") or "Agent alert detected",
                status=alert.get("action_status") or "open",
                owner=alert.get("owner") or "ai-system-owner",
                severity=alert.get("severity") or "warning",
                source_type="alert",
            )
        )
    for task in (agent_tasks.get("dead_items") if isinstance(agent_tasks.get("dead_items"), list) else [])[:3]:
        items.append(
            _incident_item(
                "Contain",
                f"Dead async task {task.get('short_request_id') or task.get('request_id') or '-'}",
                f"{task.get('task_type') or 'agent_task'} · attempts {task.get('attempts')}/{task.get('max_attempts')} · age {task.get('age_label') or '-'}",
                status="needs_triage",
                owner="ai-ops-owner",
                severity="high",
                source_type="agent_task",
                source_id=str(task.get("request_id") or ""),
                time_label=str(task.get("accepted_at") or ""),
            )
        )
    for job in (local_jobs.get("items") if isinstance(local_jobs.get("items"), list) else []):
        if job.get("status") != "failed":
            continue
        items.append(
            _incident_item(
                "Diagnose",
                f"Local job #{job.get('id')} failed",
                f"{job.get('job_type') or 'agent_job'} · root cause {job.get('root_cause') or '-'}",
                status="needs_eval_case",
                owner="system-app-owner",
                severity="high",
                source_type="job",
                source_id=str(job.get("id") or ""),
                time_label=str(job.get("updated_label") or ""),
            )
        )
    for trace in (trace_summary.get("traces") if isinstance(trace_summary.get("traces"), list) else []):
        if trace.get("status") != "failed" and not trace.get("error_message"):
            continue
        items.append(
            _incident_item(
                "Diagnose",
                trace.get("workflow_name") or trace.get("decision_type") or "Failed trace",
                f"{trace.get('root_cause') or 'trace'} · {trace.get('span_count')} spans",
                status="needs_replay",
                owner="ai-ops-owner",
                severity="high",
                source_type="trace",
                source_id=str(trace.get("trace_id") or ""),
                trace_id=str(trace.get("trace_id") or ""),
                time_label=str(trace.get("updated_label") or ""),
            )
        )
    for audit in (tool_logs.get("items") if isinstance(tool_logs.get("items"), list) else []):
        if not audit.get("error_message"):
            continue
        items.append(
            _incident_item(
                "Diagnose",
                audit.get("event_type") or "Tool audit failure",
                f"{audit.get('decision_type') or 'agent_decision'} · root cause {audit.get('root_cause') or '-'}",
                status="needs_tool_review",
                owner="tool-data-owner",
                severity="high",
                source_type="audit",
                source_id=str(audit.get("audit_id") or ""),
                trace_id=str(audit.get("trace_id") or ""),
                time_label=str(audit.get("created_label") or ""),
            )
        )
    for case in (eval_backlog.get("recent") if isinstance(eval_backlog.get("recent"), list) else []):
        if case.get("suite_status") not in {"blocking", "included"}:
            continue
        items.append(
            _incident_item(
                "Fix",
                case.get("title") or "Eval backlog case",
                f"{case.get('id') or '-'} · {case.get('suite_status_label') or case.get('suite_status') or '-'}",
                status=case.get("lifecycle_status") or "open",
                owner=case.get("owner") or "eval-owner",
                severity=case.get("severity") or "medium",
                source_type="eval_backlog",
                source_id=str(case.get("id") or ""),
                time_label=str(case.get("captured_at") or case.get("lifecycle_updated_at") or ""),
            )
        )
    stage_counts = Counter(item["stage"] for item in items)
    open_statuses = {"needs_triage", "needs_eval_case", "needs_replay", "needs_tool_review", "open"}
    return {
        "rule_id": "health-agent-incident-timeline-v1",
        "status": "needs_action" if items else "ready",
        "count": len(items),
        "open_count": sum(1 for item in items if item.get("status") in open_statuses),
        "stage_counts": [{"label": stage, "value": stage_counts.get(stage, 0)} for stage in ("Detect", "Diagnose", "Contain", "Fix")],
        "items": items[:8],
    }


def _incident_item(
    stage: str,
    title: Any,
    detail: Any,
    *,
    status: Any,
    owner: Any,
    severity: Any,
    source_type: str,
    source_id: str = "",
    trace_id: str = "",
    time_label: str = "",
) -> dict[str, str]:
    return {
        "stage": stage,
        "title": redact_inline_secrets(str(title or "-"), limit=160),
        "detail": redact_inline_secrets(str(detail or "-"), limit=220),
        "status": str(status or "open"),
        "owner": str(owner or "ai-system-owner"),
        "severity": str(severity or "medium"),
        "source_type": source_type,
        "source_id": source_id,
        "trace_id": trace_id,
        "time_label": time_label,
    }


def _async_status(settings, local_counts: dict[str, int], agent_status: dict[str, Any], agent_ops: dict[str, Any]) -> dict[str, Any]:
    status_payload = _payload(agent_status)
    ops_payload = _payload(agent_ops)
    agent_counts = _safe_counts(status_payload.get("counts"))
    active_count = _int_value(status_payload.get("active_count"))
    if not active_count:
        active_count = sum(agent_counts.get(status, 0) for status in status_payload.get("active_statuses", []))
    remote_status = "ok" if agent_status.get("ok") else "unavailable"
    ops_status = str(ops_payload.get("status") or remote_status)
    return {
        "flow_label": "app_server job → agent_server queue → app_server callback",
        "transport": "remote",
        "remote_status": remote_status,
        "ops_status": ops_status,
        "agent_base_url": settings.agent_base_url,
        "callback_base_url": settings.system_base_url,
        "callback_path": "/api/agent/async/*",
        "local_pending": local_counts.get("pending", 0),
        "local_running": local_counts.get("running", 0),
        "local_done": local_counts.get("done", 0),
        "local_failed": local_counts.get("failed", 0),
        "agent_active": active_count,
        "agent_counts": agent_counts,
        "agent_error": agent_status.get("error", ""),
    }


def _workers(agent_status: dict[str, Any], agent_ops: dict[str, Any]) -> dict[str, Any]:
    status_payload = _payload(agent_status)
    ops_payload = _payload(agent_ops)
    workers = status_payload.get("workers")
    if not isinstance(workers, list):
        workers = ops_payload.get("workers") if isinstance(ops_payload.get("workers"), list) else []
    return {
        "count": len(workers),
        "running": sum(1 for row in workers if isinstance(row, dict) and row.get("status") == "running"),
        "stale": sum(1 for row in workers if isinstance(row, dict) and row.get("status") == "stale"),
        "items": [_worker_view(row) for row in workers if isinstance(row, dict)],
    }


def _worker_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "worker_id": row.get("worker_id") or "-",
        "status": row.get("status") or row.get("stored_status") or "unknown",
        "processed_count": _int_value(row.get("processed_count")),
        "heartbeat_at": row.get("heartbeat_at") or "",
        "current_task": row.get("current_task_request_id") or "",
        "current_task_type": row.get("current_task_type") or "",
        "last_error": redact_inline_secrets(str(row.get("last_error") or ""), limit=180),
    }


def _recent_local_jobs(session: Session) -> dict[str, Any]:
    rows = session.scalars(select(AgentJob).order_by(desc(AgentJob.updated_at), desc(AgentJob.id)).limit(10)).all()
    return {
        "count": len(rows),
        "items": [_job_view(row) for row in rows],
    }


def _job_view(job: AgentJob) -> dict[str, Any]:
    payload = parse_json_object(job.payload_json)
    subject = payload.get("slot_label") or payload.get("target_date") or payload.get("date") or payload.get("event_type") or "local agent job"
    root_cause = _classify_root_cause(job.error_message, default="local_job")
    return {
        "id": job.id,
        "job_type": job.job_type,
        "status": job.status,
        "status_label": _status_label(job.status),
        "attempts": job.attempts,
        "subject": subject,
        "payload_keys": sorted(str(key) for key in payload.keys()),
        "root_cause": root_cause,
        "updated_label": _time_label(job.updated_at),
        "error_message": redact_inline_secrets(job.error_message or "", limit=220),
    }


def _agent_alerts(agent_ops: dict[str, Any], trace_summary: dict[str, Any], local_counts: dict[str, int]) -> dict[str, Any]:
    ops_payload = _payload(agent_ops)
    alerts = ops_payload.get("alerts") if isinstance(ops_payload.get("alerts"), list) else []
    items = [_alert_view(row) for row in alerts if isinstance(row, dict)]
    if not agent_ops.get("ok"):
        items.append(
            _alert_view(
            {
                "code": "agent_ops_unreachable",
                "severity": "warning",
                "message": f"Agent ops endpoint unreachable: {agent_ops.get('error') or agent_ops.get('status_code') or 'unknown'}",
                "details": "",
            }
            )
        )
    failed_jobs = local_counts.get("failed", 0)
    if failed_jobs:
        items.append(
            _alert_view(
            {
                "code": "local_agent_jobs_failed",
                "severity": "warning",
                "message": "Local agent job failures require operator review.",
                "details": f"failed jobs={failed_jobs}",
            }
            )
        )
    status = str(ops_payload.get("status") or ("degraded" if items else "ok"))
    return {
        "rule_id": "health-agent-alert-rules-v1",
        "status": status,
        "count": len(items),
        "items": items,
        "metrics": [
            {"label": "events", "value": trace_summary["event_count"]},
            {"label": "model errors", "value": trace_summary["model_error_count"]},
            {"label": "tool errors", "value": trace_summary["tool_error_count"]},
            {"label": "failed jobs", "value": failed_jobs},
        ],
    }


def _alert_view(row: dict[str, Any]) -> dict[str, Any]:
    code = str(row.get("code") or "agent_alert")
    detail_parts = [f"{key}={value}" for key, value in row.items() if key not in {"code", "severity", "message"}]
    return {
        "code": code,
        "severity": row.get("severity") or "warning",
        "message": row.get("message") or "",
        "details": " · ".join(detail_parts),
        "owner": _alert_owner(code),
        "runbook": _alert_runbook(code),
        "action_status": _alert_action_status(code),
    }


def _trace_dashboard(session: Session) -> dict[str, Any]:
    step_rows = session.scalars(select(AgentRunStep)).all()
    step_counts = Counter(row.step_type for row in step_rows)
    all_traces = session.scalars(select(AgentRunTrace)).all()
    traces = session.scalars(select(AgentRunTrace).order_by(desc(AgentRunTrace.updated_at), desc(AgentRunTrace.id)).limit(6)).all()
    trace_ids = [row.trace_id for row in traces]
    steps_by_trace = _steps_by_trace(session, trace_ids)
    model_latencies = [row.latency_ms for row in step_rows if row.step_type == "model_call" and row.latency_ms > 0]
    if not model_latencies:
        model_latencies = [row.latency_ms for row in all_traces if row.latency_ms > 0]
    trace_failures = [row for row in all_traces if row.status == "failed" or bool(row.error_message)]
    tool_steps = [row for row in step_rows if row.step_type == "tool_call"]
    tool_error_count = sum(1 for row in step_rows if row.step_type == "tool_call" and row.status in {"error", "failed"})
    model_error_count = sum(1 for row in step_rows if row.step_type == "model_call" and row.status in {"error", "failed"})
    expired_count = _trace_retention_dry_run_count(session)
    return {
        "subtitle": "workflow, model_call, tool_call 관측 상태",
        "event_count": len(step_rows),
        "trace_count": len(all_traces),
        "failed_trace_count": len(trace_failures),
        "tool_call_count": len(tool_steps),
        "step_counts": [{"label": key, "value": value} for key, value in sorted(step_counts.items())],
        "avg_model_latency_ms": int(mean(model_latencies)) if model_latencies else 0,
        "p95_model_latency_ms": _percentile(model_latencies, 95),
        "model_error_count": model_error_count,
        "tool_error_count": tool_error_count,
        "retention": {**TRACE_RETENTION_POLICY, "expired_count": expired_count},
        "traces": [_trace_view(row, steps_by_trace.get(row.trace_id, [])) for row in traces],
    }


def _steps_by_trace(session: Session, trace_ids: list[str]) -> dict[str, list[AgentRunStep]]:
    if not trace_ids:
        return {}
    rows = session.scalars(select(AgentRunStep).where(AgentRunStep.trace_id.in_(trace_ids)).order_by(AgentRunStep.id.asc())).all()
    grouped: dict[str, list[AgentRunStep]] = {}
    for row in rows:
        grouped.setdefault(row.trace_id, []).append(row)
    return grouped


def _trace_view(trace: AgentRunTrace, steps: list[AgentRunStep]) -> dict[str, Any]:
    root_cause = _classify_root_cause(trace.error_message, default=trace.source_event_type or trace.workflow_name or "trace")
    return {
        "trace_id": trace.trace_id,
        "short_trace_id": _short_id(trace.trace_id),
        "workflow_name": trace.workflow_name,
        "agent_name": trace.agent_name,
        "decision_type": trace.decision_type,
        "status": trace.status,
        "updated_label": _time_label(trace.updated_at),
        "span_count": len(steps),
        "model_id": trace.model_id,
        "model_tier": trace.model_tier,
        "provider": trace.provider,
        "latency_ms": trace.latency_ms,
        "tool_count": trace.tool_count,
        "root_cause": root_cause,
        "error_message": redact_inline_secrets(trace.error_message or "", limit=180),
        "steps": [_step_view(row) for row in steps],
    }


def _step_view(step: AgentRunStep) -> dict[str, Any]:
    return {
        "step_type": step.step_type,
        "step_name": step.step_name or step.tool_name or "-",
        "status": step.status or "-",
        "latency_ms": step.latency_ms,
        "tool_name": step.tool_name,
        "side_effect_level": step.side_effect_level,
    }


def _agent_tasks(agent_tasks: dict[str, Any]) -> dict[str, Any]:
    task_payload = _payload(agent_tasks)
    raw_tasks = task_payload.get("tasks") if isinstance(task_payload.get("tasks"), list) else []
    items = [_task_view(row) for row in raw_tasks if isinstance(row, dict)]
    status_counts = Counter(row["status"] for row in items)
    active_statuses = {"pending", "running", "callback_sent"}
    return {
        "available": bool(agent_tasks.get("ok")),
        "count": len(items),
        "active_count": sum(1 for row in items if row["status"] in active_statuses),
        "dead_count": sum(1 for row in items if row["status"] == "dead"),
        "status_counts": [{"label": key, "value": value} for key, value in sorted(status_counts.items())],
        "items": items,
        "dead_items": [row for row in items if row["status"] == "dead"],
        "error": agent_tasks.get("error") or "",
    }


def _task_view(row: dict[str, Any]) -> dict[str, Any]:
    request_id = str(row.get("request_id") or "")
    status = str(row.get("status") or "unknown")
    age_seconds = _optional_int_value(row.get("age_seconds"))
    runtime_seconds = _optional_int_value(row.get("runtime_seconds"))
    next_retry_seconds = _optional_int_value(row.get("next_retry_in_seconds"))
    payload_keys = row.get("payload_keys") if isinstance(row.get("payload_keys"), list) else []
    callback_context = row.get("callback_context") if isinstance(row.get("callback_context"), dict) else {}
    return {
        "id": _int_value(row.get("id")),
        "request_id": request_id,
        "short_request_id": _short_id(request_id),
        "task_type": row.get("task_type") or "-",
        "status": status,
        "status_label": _status_label(status),
        "attempts": _int_value(row.get("attempts")),
        "max_attempts": _int_value(row.get("max_attempts")),
        "age_seconds": age_seconds,
        "age_label": _duration_label(age_seconds),
        "runtime_seconds": runtime_seconds,
        "runtime_label": _duration_label(runtime_seconds),
        "next_retry_seconds": next_retry_seconds,
        "next_retry_label": _duration_label(next_retry_seconds),
        "accepted_at": row.get("accepted_at") or "",
        "completed_at": row.get("completed_at") or "",
        "locked_by": row.get("locked_by") or "",
        "payload_keys": sorted(str(key) for key in payload_keys),
        "callback_context": _compact_mapping(callback_context),
        "last_error": redact_inline_secrets(str(row.get("last_error") or ""), limit=220),
        "root_cause": _classify_root_cause(row.get("last_error"), default="async_task"),
    }


def _online_quality_metrics(trace_summary: dict[str, Any], agent_task_view: dict[str, Any]) -> dict[str, Any]:
    trace_count = _int_value(trace_summary.get("trace_count"))
    failed_trace_count = _int_value(trace_summary.get("failed_trace_count"))
    successful_trace_count = max(0, trace_count - failed_trace_count)
    success_rate = 100.0 if trace_count == 0 else (successful_trace_count / trace_count) * 100

    tool_call_count = _int_value(trace_summary.get("tool_call_count"))
    tool_error_count = _int_value(trace_summary.get("tool_error_count"))
    tool_error_rate = 0.0 if tool_call_count == 0 else (tool_error_count / tool_call_count) * 100

    tasks = agent_task_view.get("items") if isinstance(agent_task_view.get("items"), list) else []
    callback_latency_values = [
        row["runtime_seconds"] * 1000
        for row in tasks
        if row.get("runtime_seconds") is not None and row.get("status") in {"done", "failed", "dead"}
    ]
    queue_age_values = [row["age_seconds"] for row in tasks if row.get("age_seconds") is not None]
    callback_latency_p95_ms = _percentile(callback_latency_values, 95)
    queue_age_p95_seconds = _percentile(queue_age_values, 95)

    return {
        "rule_id": "health-agent-online-quality-v1",
        "metrics": [
            {"label": "success rate", "value": f"{success_rate:.1f}%", "target": ">= 95%"},
            {"label": "tool error rate", "value": f"{tool_error_rate:.1f}%", "target": "<= 2%"},
            {"label": "callback latency p95", "value": f"{callback_latency_p95_ms} ms", "target": "<= 2000 ms"},
            {"label": "queue age p95", "value": f"{queue_age_p95_seconds} s", "target": "<= 300 s"},
        ],
        "gates": [
            _quality_gate("success rate", success_rate, 95.0, "gte", "%"),
            _quality_gate("tool error rate", tool_error_rate, 2.0, "lte", "%"),
            _quality_gate("callback latency p95", callback_latency_p95_ms, 2000, "lte", "ms"),
            _quality_gate("queue age p95", queue_age_p95_seconds, 300, "lte", "s"),
        ],
    }


def _quality_gate(label: str, current: float, target: float, comparator: str, unit: str) -> dict[str, str]:
    passed = current >= target if comparator == "gte" else current <= target
    target_label = f">= {target:g}{unit}" if comparator == "gte" else f"<= {target:g}{unit}"
    current_label = f"{current:.1f}{unit}" if unit == "%" else f"{int(current)} {unit}"
    return {
        "label": label,
        "status": "pass" if passed else "watch",
        "target": target_label,
        "current": current_label,
    }


def _eval_backlog_summary() -> dict[str, Any]:
    canonical_ids = _canonical_eval_case_ids()
    if not EVAL_BACKLOG_PATH.exists():
        return {
            "path": str(EVAL_BACKLOG_PATH),
            "dataset_path": str(EVAL_DATASET_PATH),
            "count": 0,
            "included_count": 0,
            "excluded_count": 0,
            "blocking_count": 0,
            "lifecycle_counts": _lifecycle_counts([]),
            "recent": [],
        }
    try:
        cases = load_eval_case_file(EVAL_BACKLOG_PATH, missing_ok=True)
    except SystemExit as exc:
        return {
            "path": str(EVAL_BACKLOG_PATH),
            "dataset_path": str(EVAL_DATASET_PATH),
            "count": 0,
            "included_count": 0,
            "excluded_count": 0,
            "blocking_count": 0,
            "lifecycle_counts": _lifecycle_counts([]),
            "recent": [],
            "error": str(exc),
        }
    rows = _eval_backlog_case_rows(cases, canonical_ids)
    recent = []
    for row in reversed(rows[-5:]):
        case = row["case"]
        source = case.get("source") if isinstance(case.get("source"), dict) else {}
        recent.append(
            {
                "id": case.get("id") or "-",
                "title": case.get("title") or "Untitled eval case",
                "owner": case.get("owner") or "eval-owner",
                "severity": case.get("severity") or "medium",
                "captured_at": source.get("captured_at") or "",
                "lifecycle_status": row["lifecycle_status"],
                "lifecycle_updated_at": row["lifecycle_updated_at"],
                "lifecycle_reason": row["lifecycle_reason"],
                "suite_status": row["suite_status"],
                "suite_status_label": row["suite_status_label"],
                "suite_reason": row["suite_reason"],
            }
        )
    return {
        "path": str(EVAL_BACKLOG_PATH),
        "dataset_path": str(EVAL_DATASET_PATH),
        "count": len(cases),
        "included_count": sum(1 for row in rows if row["included"]),
        "excluded_count": sum(1 for row in rows if not row["included"]),
        "blocking_count": sum(1 for row in rows if row["suite_status"] == "blocking"),
        "lifecycle_counts": _lifecycle_counts(cases),
        "recent": recent,
    }


def _canonical_eval_case_ids() -> set[str]:
    try:
        cases = load_eval_case_file(EVAL_DATASET_PATH, missing_ok=True)
    except SystemExit:
        return set()
    return {case_id for case in cases if (case_id := eval_case_id(case))}


def _eval_backlog_case_rows(cases: list[dict[str, Any]], canonical_ids: set[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids = set(canonical_ids)
    for case in cases:
        case_id = eval_case_id(case)
        findings = eval_case_schema_findings(case)
        lifecycle = case.get("lifecycle") if isinstance(case.get("lifecycle"), dict) else {}
        lifecycle_status = eval_case_lifecycle_status(case)
        lifecycle_reason = str(lifecycle.get("reason") or "")
        lifecycle_updated_at = str(lifecycle.get("updated_at") or "")
        if findings:
            status = "invalid"
            reason = ", ".join(str(finding.get("code") or "schema_error") for finding in findings[:3])
            included = False
        elif case_id and case_id in seen_ids:
            status = "duplicate_excluded"
            reason = "duplicate stable id already exists in canonical dataset or earlier backlog case"
            included = False
        elif is_blocking_backlog_case(case):
            status = "blocking"
            reason = "included in eval suite and blocks CI until owner review/regression evidence"
            included = True
            if case_id:
                seen_ids.add(case_id)
        elif str(case.get("severity") or "") in {"critical", "high"} and lifecycle_status == "cleared":
            status = "cleared"
            reason = "included in eval suite but cleared from CI blocking by lifecycle review"
            included = True
            if case_id:
                seen_ids.add(case_id)
        else:
            status = "included"
            reason = "included in eval suite"
            included = True
            if case_id:
                seen_ids.add(case_id)
        rows.append(
            {
                "case": case,
                "included": included,
                "lifecycle_status": lifecycle_status,
                "lifecycle_reason": lifecycle_reason,
                "lifecycle_updated_at": lifecycle_updated_at,
                "suite_status": status,
                "suite_status_label": {
                    "included": "eval suite included",
                    "blocking": "CI blocking",
                    "cleared": "cleared from CI gate",
                    "duplicate_excluded": "duplicate excluded",
                    "invalid": "schema invalid",
                }.get(status, status),
                "suite_reason": reason,
            }
        )
    return rows


def _lifecycle_counts(cases: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(eval_case_lifecycle_status(case) for case in cases)
    return {status: counts.get(status, 0) for status in ("open", "reviewed", "cleared")}


def _classify_root_cause(message: Any, *, default: str) -> str:
    text = str(message or "").lower()
    if not text:
        return default
    if any(token in text for token in ("callback", "connection", "connect", "http", "timeout", "readtimeout")):
        return "callback_delivery"
    if any(token in text for token in ("bedrock", "anthropic", "model", "provider", "winerror 10013")):
        return "model_provider"
    if any(token in text for token in ("tool", "permission", "unauthorized", "forbidden", "policy")):
        return "tool_policy"
    if any(token in text for token in ("retry", "lock", "dead", "attempt")):
        return "async_retry_budget"
    if any(token in text for token in ("eval", "judge", "groundedness", "hallucination")):
        return "evaluation_gap"
    return default


def _alert_owner(code: str) -> str:
    lowered = code.lower()
    if "worker" in lowered:
        return "agent-platform-owner"
    if any(token in lowered for token in ("dead", "callback", "provider", "bedrock", "async")):
        return "ai-ops-owner"
    if "local" in lowered:
        return "system-app-owner"
    return "ai-system-owner"


def _alert_runbook(code: str) -> str:
    lowered = code.lower()
    if "eval" in lowered:
        return "docs/PRODUCTION_READINESS.md#evaluation"
    if any(token in lowered for token in ("worker", "dead", "callback", "async")):
        return "docs/PRODUCTION_READINESS.md#incident-runbook"
    return "docs/PRODUCTION_READINESS.md#observability"


def _alert_action_status(code: str) -> str:
    lowered = code.lower()
    if any(token in lowered for token in ("dead", "failed", "provider", "callback", "unreachable")):
        return "needs_triage"
    if any(token in lowered for token in ("stale", "degraded")):
        return "monitoring"
    return "open"


def _trace_retention_dry_run_count(session: Session) -> int:
    return trace_retention_expired_count(session)


def _tool_catalog() -> dict[str, Any]:
    tools = [_tool_catalog_item(tool) for tool in ToolCatalog.available_tools_payload()]
    domain_counts = Counter(row["domain"] for row in tools)
    risk_counts = Counter(row["risk"] for row in tools)
    freshness_counts = Counter(row["freshness_status"] for row in tools)
    return {
        "rule_id": "health-agent-tool-catalog-v2",
        "subtitle": "owner/freshness/access/risk",
        "count": len(tools),
        "domain_counts": [{"label": key, "value": value} for key, value in sorted(domain_counts.items())],
        "risk_counts": [{"label": f"risk {key}", "value": value} for key, value in sorted(risk_counts.items())],
        "freshness_counts": [{"label": f"freshness {key}", "value": value} for key, value in sorted(freshness_counts.items())],
        "data_sources": _data_freshness_sources(),
        "items": tools,
    }


def _tool_catalog_item(tool: dict[str, Any]) -> dict[str, Any]:
    name = str(tool.get("name") or "")
    domain = _tool_domain(name)
    side_effect = _tool_side_effect(name)
    risk = _tool_risk(name, side_effect)
    freshness = _tool_freshness_probe(name, "agent_app/tools/catalog.py")
    handoff_required = requires_human_handoff(name)
    return {
        "name": name,
        "title": tool.get("title") or name,
        "domain": domain,
        "owner": _tool_owner(domain),
        "source": "agent_app/tools/catalog.py",
        "freshness": freshness["label"],
        "freshness_status": freshness["status"],
        "freshness_checked_at": freshness["checked_at"],
        "access": "agent_read",
        "side_effect": side_effect,
        "risk": risk,
        "human_handoff_required": handoff_required,
        "human_handoff_label": "required" if handoff_required else "not required",
        "description": tool.get("description") or "",
    }


def _tool_freshness_probe(tool_name: str, source: str) -> dict[str, str]:
    source_path = (REPO_ROOT / source).resolve()
    checked_at = utc_now().isoformat()
    if not source_path.exists():
        return {"status": "missing_source", "label": "missing source", "checked_at": checked_at}
    try:
        source_text = source_path.read_text(encoding="utf-8")
    except OSError:
        return {"status": "unreadable_source", "label": "unreadable source", "checked_at": checked_at}
    if tool_name and tool_name not in source_text:
        return {"status": "needs_review", "label": "catalog mismatch", "checked_at": checked_at}
    return {"status": "ok", "label": "freshness ok", "checked_at": checked_at}


def _data_freshness_sources() -> list[dict[str, str]]:
    sources = [
        ("eval_dataset", EVAL_DATASET_PATH),
        ("eval_backlog", EVAL_BACKLOG_PATH),
        ("change_log", CHANGE_LOG_PATH),
    ]
    rows: list[dict[str, str]] = []
    checked_at = utc_now().isoformat()
    for label, path in sources:
        source_path = (REPO_ROOT / path).resolve() if not path.is_absolute() else path
        if not source_path.exists():
            rows.append({"label": label, "path": str(path), "status": "missing", "checked_at": checked_at})
            continue
        try:
            if source_path.suffix.lower() == ".json":
                json.loads(source_path.read_text(encoding="utf-8"))
            rows.append({"label": label, "path": str(path), "status": "ok", "checked_at": checked_at})
        except (OSError, ValueError):
            rows.append({"label": label, "path": str(path), "status": "needs_review", "checked_at": checked_at})
    return rows


def _tool_execution_logs(session: Session) -> dict[str, Any]:
    audits = session.scalars(select(AgentDecisionAudit).order_by(desc(AgentDecisionAudit.created_at), desc(AgentDecisionAudit.id)).limit(12)).all()
    rows = [_audit_log_view(row) for row in audits]
    return {
        "subtitle": "AgentResponse audit와 source label 확인",
        "count": len(rows),
        "items": rows,
    }


def _audit_log_view(audit: AgentDecisionAudit) -> dict[str, Any]:
    structured = parse_json_object(audit.structured_payload)
    tools = _tools_from_structured_payload(structured)
    return {
        "audit_id": audit.id,
        "trace_id": audit.trace_id,
        "event_type": "agent_call_failed" if audit.error_message else "health_tool_call" if tools else "agent_decision_audit",
        "decision_type": audit.decision_type,
        "agent_name": audit.agent_name,
        "source_event_type": audit.source_event_type,
        "created_label": _time_label(audit.created_at),
        "summary": _audit_summary_for_display(audit),
        "error_message": redact_inline_secrets(audit.error_message or "", limit=220),
        "root_cause": _classify_root_cause(audit.error_message, default="tool_audit"),
        "tools": tools,
    }


def _audit_summary_for_display(audit: AgentDecisionAudit) -> str:
    if audit.human_summary:
        if audit.human_summary.startswith("clinical text redacted ·"):
            return audit.human_summary
        return redacted_clinical_text_label(audit.human_summary)
    if audit.error_message:
        return redact_inline_secrets(audit.error_message, limit=220)
    return "Agent decision audit"


def _tools_from_structured_payload(structured: dict[str, Any]) -> list[dict[str, Any]]:
    calls = structured.get("tool_calls")
    if not isinstance(calls, list):
        single_call = structured.get("tool_call")
        calls = [single_call] if isinstance(single_call, dict) else []
    calls = [call for call in calls if isinstance(call, dict)]
    results = structured.get("tool_results")
    results = [result for result in results if isinstance(result, dict)] if isinstance(results, list) else []
    rows: list[dict[str, Any]] = []
    for index, call in enumerate(calls):
        name = str(call.get("name") or "")
        result = _matching_result(name, results, index)
        side_effect = _tool_side_effect(name)
        rows.append(
            {
                "name": name or "unknown_tool",
                "source_label": _tool_owner(_tool_domain(name)),
                "status": str(result.get("status") or "planned"),
                "risk": _tool_risk(name, side_effect),
                "side_effect": side_effect,
                "error": redact_inline_secrets(str(result.get("error") or ""), limit=140),
            }
        )
    for result in results:
        name = str(result.get("tool_name") or "")
        if name and not any(row["name"] == name for row in rows):
            side_effect = _tool_side_effect(name)
            rows.append(
                {
                    "name": name,
                    "source_label": _tool_owner(_tool_domain(name)),
                    "status": str(result.get("status") or "result"),
                    "risk": _tool_risk(name, side_effect),
                    "side_effect": side_effect,
                    "error": redact_inline_secrets(str(result.get("error") or ""), limit=140),
                }
            )
    return rows


def _matching_result(tool_name: str, results: list[dict[str, Any]], index: int) -> dict[str, Any]:
    if index < len(results):
        candidate = results[index]
        if not tool_name or str(candidate.get("tool_name") or "") in {"", tool_name}:
            return candidate
    return next((result for result in results if str(result.get("tool_name") or "") == tool_name), {})


def _local_job_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(AgentJob.status, func.count(AgentJob.id)).group_by(AgentJob.status)).all()
    return {str(status): int(count) for status, count in rows}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}


def _safe_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        counts[str(key)] = _int_value(count)
    return counts


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _optional_int_value(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _percentile(values: list[int | float], percentile: int) -> int:
    clean_values = sorted(float(value) for value in values if value is not None and value >= 0)
    if not clean_values:
        return 0
    if len(clean_values) == 1:
        return int(round(clean_values[0]))
    rank = (len(clean_values) - 1) * (max(0, min(percentile, 100)) / 100)
    lower_index = int(rank)
    upper_index = min(lower_index + 1, len(clean_values) - 1)
    fraction = rank - lower_index
    value = clean_values[lower_index] + (clean_values[upper_index] - clean_values[lower_index]) * fraction
    return int(round(value))


def _duration_label(value: int | None) -> str:
    if value is None:
        return "-"
    if value < 60:
        return f"{value}s"
    minutes, seconds = divmod(value, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m"


def _compact_mapping(value: dict[str, Any]) -> str:
    if not value:
        return "-"
    pieces = []
    for key, item in sorted(value.items()):
        if item in ("", None, [], {}):
            continue
        pieces.append(f"{key}={redact_inline_secrets(str(item), limit=80)}")
    return " · ".join(pieces[:6]) if pieces else "-"


def _status_label(status: str) -> str:
    return {
        "pending": "대기",
        "running": "전송",
        "done": "완료",
        "failed": "실패",
        "dead": "dead",
        "callback_sent": "callback",
    }.get(status, status)


def _time_label(value: datetime | None) -> str:
    return value.strftime("%m-%d %H:%M") if value else "-"


def _short_id(value: str) -> str:
    if len(value) <= 16:
        return value
    return f"{value[:8]}...{value[-6:]}"


def _tool_domain(name: str) -> str:
    tool_name = canonical_tool_name(name)
    if tool_name in {
        SEARCH_NUTRITION_FOOD_CANDIDATES,
        CREATE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
        GET_NUTRITION_MEAL_RECORD_LIST,
        GET_NUTRITION_DAILY_SUMMARY,
        UPSERT_NUTRITION_PREFERENCE_FACT,
        GET_NUTRITION_PREFERENCE_SUMMARY,
    }:
        return "nutrition"
    if tool_name in POLICY_TOOLS:
        return "policy"
    if tool_name in {GET_PRO_CTCAE_QUESTIONNAIRE, GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}:
        return "safety"
    if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return "medication"
    return "shared"


def _tool_owner(domain: str) -> str:
    return {
        "nutrition": "nutrition-PoC",
        "policy": "DA-drug-policy",
        "safety": "DA-drug-safety",
        "medication": "DA-drug-medication",
    }.get(domain, "DA-drug")


def _tool_side_effect(name: str) -> str:
    tool_name = canonical_tool_name(name)
    if tool_name in {
        CREATE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_MEAL_RECORD,
        DELETE_NUTRITION_MEAL_RECORD,
        UPDATE_NUTRITION_FOOD_RECORD,
        DELETE_NUTRITION_FOOD_RECORD,
        UPSERT_NUTRITION_PREFERENCE_FACT,
    }:
        return "record_write"
    if tool_name == UPDATE_MEDICATION_DOSE_EVENT_STATUS:
        return "state_write"
    if tool_name in POLICY_TOOLS:
        return "candidate_write"
    return "read_only"


def _tool_risk(name: str, side_effect: str) -> str:
    tool_name = canonical_tool_name(name)
    if tool_name in POLICY_TOOLS:
        return "high"
    if side_effect in {"record_write", "state_write", "candidate_write"}:
        return "medium"
    if tool_name in {GET_PRO_CTCAE_QUESTIONNAIRE, GET_MEDICATION_SIDE_EFFECT_ASSESSMENT}:
        return "medium"
    return "low"
