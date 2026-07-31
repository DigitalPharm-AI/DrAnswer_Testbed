from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LoadBudget:
    concurrency_target: int
    max_error_rate: float
    p95_health_ms: int
    p95_async_accept_ms: int
    p95_ui_status_ms: int


@dataclass(frozen=True)
class CostBudget:
    daily_budget_usd: float
    eval_budget_usd: float
    input_usd_per_1m_tokens: float
    output_usd_per_1m_tokens: float


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    if q <= 0:
        return min(values)
    if q >= 1:
        return max(values)
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_probe_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        workflow = str(sample.get("workflow") or "unknown")
        by_workflow.setdefault(workflow, []).append(sample)

    workflows: dict[str, dict[str, Any]] = {}
    for workflow, rows in by_workflow.items():
        durations = [float(row.get("elapsed_ms") or 0) for row in rows]
        failures = [row for row in rows if not row.get("ok")]
        status_codes = _count_values(str(int(row.get("status_code") or 0)) for row in rows)
        errors = _count_values(str(row.get("error") or "") for row in rows if row.get("error"))
        workflows[workflow] = {
            "count": len(rows),
            "failure_count": len(failures),
            "error_rate": len(failures) / len(rows) if rows else 0.0,
            "p50_ms": round(percentile(durations, 0.50) or 0, 2),
            "p95_ms": round(percentile(durations, 0.95) or 0, 2),
            "max_ms": round(max(durations) if durations else 0, 2),
            "status_codes": status_codes,
            "errors": errors,
        }

    total_count = len(samples)
    total_failures = sum(1 for sample in samples if not sample.get("ok"))
    return {
        "total_count": total_count,
        "failure_count": total_failures,
        "error_rate": total_failures / total_count if total_count else 0.0,
        "workflows": workflows,
    }


def _count_values(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        if not value:
            continue
        counts[value] = counts.get(value, 0) + 1
    return counts


def evaluate_load_budget(summary: dict[str, Any], budget: LoadBudget) -> dict[str, Any]:
    alerts: list[dict[str, Any]] = []
    error_rate = float(summary.get("error_rate") or 0)
    if error_rate > budget.max_error_rate:
        alerts.append(
            {
                "code": "load_error_rate_exceeded",
                "severity": "critical",
                "actual": error_rate,
                "threshold": budget.max_error_rate,
            }
        )
    workflow_budgets = {
        "system_health": budget.p95_health_ms,
        "agent_health": budget.p95_health_ms,
        "agent_readiness": budget.p95_async_accept_ms,
        "ui_status": budget.p95_ui_status_ms,
    }
    workflows = summary.get("workflows") if isinstance(summary.get("workflows"), dict) else {}
    for workflow, threshold_ms in workflow_budgets.items():
        workflow_summary = workflows.get(workflow)
        if not isinstance(workflow_summary, dict):
            continue
        p95_ms = float(workflow_summary.get("p95_ms") or 0)
        if p95_ms > threshold_ms:
            alerts.append(
                {
                    "code": "load_p95_latency_exceeded",
                    "severity": "warning",
                    "workflow": workflow,
                    "actual_ms": p95_ms,
                    "threshold_ms": threshold_ms,
                }
            )
    return {
        "status": "critical" if any(alert["severity"] == "critical" for alert in alerts) else "degraded" if alerts else "ok",
        "alerts": alerts,
    }


def estimate_model_cost_usd(
    *,
    input_tokens: int,
    output_tokens: int,
    input_usd_per_1m_tokens: float,
    output_usd_per_1m_tokens: float,
) -> float:
    input_cost = (max(0, input_tokens) / 1_000_000) * max(0.0, input_usd_per_1m_tokens)
    output_cost = (max(0, output_tokens) / 1_000_000) * max(0.0, output_usd_per_1m_tokens)
    return round(input_cost + output_cost, 6)


def evaluate_cost_budget(
    *,
    expected_runs_per_day: int,
    average_input_tokens: int,
    average_output_tokens: int,
    budget: CostBudget,
) -> dict[str, Any]:
    per_run_usd = estimate_model_cost_usd(
        input_tokens=average_input_tokens,
        output_tokens=average_output_tokens,
        input_usd_per_1m_tokens=budget.input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=budget.output_usd_per_1m_tokens,
    )
    daily_estimate_usd = round(per_run_usd * max(0, expected_runs_per_day), 6)
    alerts: list[dict[str, Any]] = []
    if budget.input_usd_per_1m_tokens <= 0 or budget.output_usd_per_1m_tokens <= 0:
        alerts.append(
            {
                "code": "model_token_prices_not_configured",
                "severity": "warning",
                "message": "Set AGENT_COST_INPUT_USD_PER_1M_TOKENS and AGENT_COST_OUTPUT_USD_PER_1M_TOKENS before using cost as a launch gate.",
            }
        )
    if budget.daily_budget_usd > 0 and daily_estimate_usd > budget.daily_budget_usd:
        alerts.append(
            {
                "code": "daily_cost_budget_exceeded",
                "severity": "critical",
                "actual_usd": daily_estimate_usd,
                "threshold_usd": budget.daily_budget_usd,
            }
        )
    return {
        "status": "critical" if any(alert["severity"] == "critical" for alert in alerts) else "degraded" if alerts else "ok",
        "per_run_usd": per_run_usd,
        "daily_estimate_usd": daily_estimate_usd,
        "alerts": alerts,
    }
