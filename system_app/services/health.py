from __future__ import annotations

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.readiness_budget import CostBudget, LoadBudget
from shared.redaction import redacted_clinical_text_label, safe_exception_summary
from shared.settings import get_settings

SUPPORTED_LLM_PROVIDERS = {"bedrock", "bedrock_anthropic", "anthropic_bedrock"}


async def collect_system_health(session: Session) -> dict:
    settings = get_settings()
    database = _database_health(session)
    workbook = _workbook_health(settings.prompt_workbook_path)
    policy_workbook = _workbook_health(settings.policy_workbook_path)
    agent_server = await _agent_server_health(settings.agent_base_url)
    agent_async = await _agent_async_health(settings.agent_base_url, settings.internal_api_token)
    model_tier = settings.llm_model_tier
    llm_provider_supported = _llm_provider_supported(settings)
    llm_credentials_required = _llm_credentials_required(settings)
    credential_sources = _llm_credential_sources(settings)
    llm_configured = llm_provider_supported and (bool(credential_sources) or not llm_credentials_required)
    llm = {
        "provider": settings.llm_provider,
        "provider_supported": llm_provider_supported,
        "model_tier": model_tier,
        "model": settings.model_id_for_tier(model_tier),
        "base_url": "aws-bedrock" if llm_provider_supported else "unsupported",
        "api_key_configured": llm_configured,
        "credentials_required": llm_credentials_required,
        "status": "configured" if llm_configured else "unsupported_provider" if not llm_provider_supported else "missing_credentials",
        "credential_sources": credential_sources,
    }
    if llm_credentials_required and not credential_sources:
        llm["credential_hint"] = "Configure AWS_BEARER_TOKEN_BEDROCK, AWS_PROFILE, or AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY."
    internal_api = {
        "required": settings.is_production(),
        "token_configured": bool(settings.internal_api_token),
    }
    budgets = _budget_health(settings)
    migrations = _migration_health(session)
    warnings = _health_warnings(agent_server, agent_async, llm)
    status = (
        "ok"
        if (
            database["ok"]
            and workbook["readable"]
            and policy_workbook["readable"]
            and agent_server["reachable"]
            and agent_async["reachable"]
            and agent_async["worker_available"]
            and llm["api_key_configured"]
        )
        else "degraded"
    )
    return {
        "status": status,
        "warnings": warnings,
        "database": database,
        "agent_server": agent_server,
        "agent_async": agent_async,
        "llm": llm,
        "budgets": budgets,
        "internal_api": internal_api,
        "prompt_workbook": workbook,
        "policy_workbook": policy_workbook,
        "migrations": migrations,
    }


def _budget_health(settings) -> dict:
    load_budget = LoadBudget(
        concurrency_target=settings.pilot_load_concurrency_target,
        max_error_rate=settings.pilot_max_error_rate,
        p95_health_ms=settings.pilot_health_p95_ms,
        p95_async_accept_ms=settings.pilot_async_accept_p95_ms,
        p95_phr_register_ms=settings.pilot_phr_register_p95_ms,
    )
    cost_budget = CostBudget(
        daily_budget_usd=settings.agent_daily_cost_budget_usd,
        eval_budget_usd=settings.agent_eval_cost_budget_usd,
        input_usd_per_1m_tokens=settings.agent_cost_input_usd_per_1m_tokens,
        output_usd_per_1m_tokens=settings.agent_cost_output_usd_per_1m_tokens,
    )
    cost_prices_configured = cost_budget.input_usd_per_1m_tokens > 0 and cost_budget.output_usd_per_1m_tokens > 0
    return {
        "load": {
            "concurrency_target": load_budget.concurrency_target,
            "max_error_rate": load_budget.max_error_rate,
            "p95_health_ms": load_budget.p95_health_ms,
            "p95_async_accept_ms": load_budget.p95_async_accept_ms,
            "p95_phr_register_ms": load_budget.p95_phr_register_ms,
        },
        "cost": {
            "daily_budget_usd": cost_budget.daily_budget_usd,
            "eval_budget_usd": cost_budget.eval_budget_usd,
            "token_prices_configured": cost_prices_configured,
            "input_usd_per_1m_tokens": cost_budget.input_usd_per_1m_tokens,
            "output_usd_per_1m_tokens": cost_budget.output_usd_per_1m_tokens,
        },
        "warnings": [] if cost_prices_configured else ["model_token_prices_not_configured"],
    }


def _llm_credentials_configured(settings) -> bool:
    return bool(_llm_credential_sources(settings))


def _llm_provider_supported(settings) -> bool:
    return settings.llm_provider.strip().lower() in SUPPORTED_LLM_PROVIDERS


def _llm_credentials_required(settings) -> bool:
    return _llm_provider_supported(settings)


def _llm_credential_sources(settings) -> list[str]:
    sources: list[str] = []
    if settings.aws_bearer_token_bedrock:
        sources.append("AWS_BEARER_TOKEN_BEDROCK")
    if settings.aws_profile:
        sources.append("AWS_PROFILE")
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        sources.append("AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY")
    return sources


def _health_warnings(agent_server: dict, agent_async: dict, llm: dict) -> list[str]:
    warnings: list[str] = []
    if not agent_server["reachable"]:
        warnings.append("agent_server_unreachable")
    if not agent_async["reachable"]:
        warnings.append("agent_async_endpoint_unreachable")
    elif not agent_async["worker_available"]:
        warnings.append("agent_async_worker_unavailable")
    if agent_async.get("pending_count", 0) and not agent_async.get("running_worker_count", 0):
        warnings.append("agent_async_pending_without_running_worker")
    if agent_async.get("dead_count", 0):
        warnings.append("agent_async_dead_tasks_present")
    if not llm.get("provider_supported", True):
        warnings.append("llm_provider_unsupported")
    elif not llm["api_key_configured"]:
        warnings.append("llm_credentials_missing")
    return warnings


def _database_health(session: Session) -> dict:
    try:
        session.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # pragma: no cover - depends on broken DB state
        return {"ok": False, "error": safe_exception_summary(exc)}
    return {"ok": True}


def _workbook_health(path) -> dict:
    exists = path.exists()
    readable = False
    error = ""
    try:
        if exists:
            with path.open("rb"):
                readable = True
        else:
            readable = path.parent.exists()
    except OSError as exc:
        error = safe_exception_summary(exc)
    payload = {"path": str(path), "exists": exists, "readable": readable}
    if error:
        payload["error"] = error
    return payload


async def _agent_server_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            response = await client.get(url)
        return {"url": url, "reachable": response.is_success, "status_code": response.status_code}
    except httpx.HTTPError as exc:
        return {"url": url, "reachable": False, "error": exc.__class__.__name__}


async def _agent_async_health(base_url: str, internal_api_token: str | None) -> dict:
    url = f"{base_url.rstrip('/')}/agent/async/tasks/status"
    headers = {"X-Internal-Api-Token": internal_api_token} if internal_api_token else {}
    try:
        async with httpx.AsyncClient(timeout=1.0, trust_env=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        return _empty_agent_async_health(url, reachable=False, error=exc.__class__.__name__)

    payload = _json_response_payload(response)
    counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
    counts = {str(status): int(count) for status, count in counts.items() if isinstance(count, int)}
    workers = _safe_worker_health_rows(payload.get("workers") if isinstance(payload.get("workers"), list) else [])
    active_count = payload.get("active_count")
    if not isinstance(active_count, int):
        active_statuses = payload.get("active_statuses") if isinstance(payload.get("active_statuses"), list) else []
        active_count = sum(counts.get(str(status), 0) for status in active_statuses)
    running_worker_count = sum(1 for worker in workers if isinstance(worker, dict) and worker.get("status") == "running")
    stale_worker_count = sum(1 for worker in workers if isinstance(worker, dict) and worker.get("status") == "stale")
    pending_count = counts.get("pending", 0)
    dead_count = counts.get("dead", 0)
    warnings = []
    if response.status_code == 401:
        warnings.append("invalid_internal_api_token")
    if response.is_success and not workers:
        warnings.append("no_worker_heartbeat")
    if pending_count and running_worker_count == 0:
        warnings.append("pending_tasks_without_running_worker")
    if dead_count:
        warnings.append("dead_tasks_present")
    return {
        "url": url,
        "reachable": response.is_success,
        "status_code": response.status_code,
        "counts": counts,
        "active_count": active_count,
        "pending_count": pending_count,
        "dead_count": dead_count,
        "worker_count": len(workers),
        "running_worker_count": running_worker_count,
        "stale_worker_count": stale_worker_count,
        "worker_available": running_worker_count > 0,
        "workers": workers,
        "warnings": warnings,
    }


def _empty_agent_async_health(url: str, *, reachable: bool, error: str = "") -> dict:
    payload = {
        "url": url,
        "reachable": reachable,
        "counts": {},
        "active_count": 0,
        "pending_count": 0,
        "dead_count": 0,
        "worker_count": 0,
        "running_worker_count": 0,
        "stale_worker_count": 0,
        "worker_available": False,
        "workers": [],
        "warnings": [],
    }
    if error:
        payload["error"] = error
    return payload


def _json_response_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _safe_worker_health_rows(workers: list) -> list[dict]:
    safe_rows = []
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        row = dict(worker)
        row["last_error"] = _safe_error_text(row.get("last_error"))
        safe_rows.append(row)
    return safe_rows


def _safe_error_text(value) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    if "clinical text redacted" in text_value:
        return text_value
    if len(text_value) <= 80 and all(char.isascii() and (char.isalnum() or char in "_:-.") for char in text_value):
        return text_value
    return redacted_clinical_text_label(text_value, key="last_error")


def _migration_health(session: Session) -> dict:
    try:
        versions = session.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
    except Exception:
        versions = []
    return {"applied_versions": versions}
