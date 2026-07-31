from __future__ import annotations

from shared.readiness_budget import (
    CostBudget,
    LoadBudget,
    estimate_model_cost_usd,
    evaluate_cost_budget,
    evaluate_load_budget,
    summarize_probe_samples,
)


def test_summarize_probe_samples_and_load_budget_pass():
    samples = [
        {"workflow": "system_health", "ok": True, "elapsed_ms": 100, "status_code": 200},
        {"workflow": "system_health", "ok": True, "elapsed_ms": 120, "status_code": 200},
        {"workflow": "agent_readiness", "ok": True, "elapsed_ms": 300, "status_code": 200},
        {"workflow": "ui_status", "ok": True, "elapsed_ms": 90, "status_code": 200},
    ]

    summary = summarize_probe_samples(samples)
    result = evaluate_load_budget(
        summary,
        LoadBudget(
            concurrency_target=50,
            max_error_rate=0.01,
            p95_health_ms=1000,
            p95_async_accept_ms=2000,
            p95_ui_status_ms=2000,
        ),
    )

    assert summary["total_count"] == 4
    assert summary["workflows"]["system_health"]["p95_ms"] >= 100
    assert summary["workflows"]["system_health"]["status_codes"] == {"200": 2}
    assert result == {"status": "ok", "alerts": []}


def test_load_budget_flags_error_rate_and_latency():
    summary = summarize_probe_samples(
        [
            {"workflow": "system_health", "ok": True, "elapsed_ms": 100, "status_code": 200},
            {"workflow": "system_health", "ok": False, "elapsed_ms": 2500, "status_code": 500, "error": "HTTPStatusError"},
            {"workflow": "agent_readiness", "ok": True, "elapsed_ms": 3000, "status_code": 200},
        ]
    )

    result = evaluate_load_budget(
        summary,
        LoadBudget(
            concurrency_target=50,
            max_error_rate=0.01,
            p95_health_ms=1000,
            p95_async_accept_ms=2000,
            p95_ui_status_ms=2000,
        ),
    )

    codes = {alert["code"] for alert in result["alerts"]}
    assert result["status"] == "critical"
    assert "load_error_rate_exceeded" in codes
    assert "load_p95_latency_exceeded" in codes


def test_cost_budget_estimates_without_hard_coded_provider_prices():
    cost = estimate_model_cost_usd(
        input_tokens=1_000_000,
        output_tokens=500_000,
        input_usd_per_1m_tokens=1.25,
        output_usd_per_1m_tokens=10.0,
    )

    assert cost == 6.25


def test_cost_budget_flags_missing_prices_and_daily_budget_excess():
    missing_prices = evaluate_cost_budget(
        expected_runs_per_day=100,
        average_input_tokens=1000,
        average_output_tokens=1000,
        budget=CostBudget(
            daily_budget_usd=50,
            eval_budget_usd=10,
            input_usd_per_1m_tokens=0,
            output_usd_per_1m_tokens=0,
        ),
    )
    over_budget = evaluate_cost_budget(
        expected_runs_per_day=10_000,
        average_input_tokens=10_000,
        average_output_tokens=10_000,
        budget=CostBudget(
            daily_budget_usd=1,
            eval_budget_usd=10,
            input_usd_per_1m_tokens=5,
            output_usd_per_1m_tokens=15,
        ),
    )

    assert missing_prices["status"] == "degraded"
    assert missing_prices["alerts"][0]["code"] == "model_token_prices_not_configured"
    assert over_budget["status"] == "critical"
    assert over_budget["alerts"][0]["code"] == "daily_cost_budget_exceeded"
