from __future__ import annotations

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from shared.settings import get_settings


async def collect_system_health(session: Session) -> dict:
    settings = get_settings()
    database = _database_health(session)
    workbook = _workbook_health(settings.prompt_workbook_path)
    policy_workbook = _workbook_health(settings.policy_workbook_path)
    agent_server = await _agent_server_health(settings.agent_base_url)
    model_tier = settings.llm_model_tier
    llm = {
        "provider": settings.llm_provider,
        "model_tier": model_tier,
        "model": settings.model_id_for_tier(model_tier),
        "base_url": "aws-bedrock",
        "api_key_configured": _llm_credentials_configured(settings),
    }
    internal_api = {
        "required": settings.is_production(),
        "token_configured": bool(settings.internal_api_token),
    }
    migrations = _migration_health(session)
    status = (
        "ok"
        if database["ok"] and workbook["readable"] and policy_workbook["readable"] and agent_server["reachable"] and llm["api_key_configured"]
        else "degraded"
    )
    return {
        "status": status,
        "database": database,
        "agent_server": agent_server,
        "llm": llm,
        "internal_api": internal_api,
        "prompt_workbook": workbook,
        "policy_workbook": policy_workbook,
        "migrations": migrations,
    }


def _llm_credentials_configured(settings) -> bool:
    return bool(
        settings.aws_bearer_token_bedrock
        or settings.aws_profile
        or (settings.aws_access_key_id and settings.aws_secret_access_key)
    )


def _database_health(session: Session) -> dict:
    try:
        session.execute(text("SELECT 1")).scalar_one()
    except Exception as exc:  # pragma: no cover - depends on broken DB state
        return {"ok": False, "error": str(exc)}
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
        error = str(exc)
    payload = {"path": str(path), "exists": exists, "readable": readable}
    if error:
        payload["error"] = error
    return payload


async def _agent_server_health(base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            response = await client.get(url)
        return {"url": url, "reachable": response.is_success, "status_code": response.status_code}
    except httpx.HTTPError as exc:
        return {"url": url, "reachable": False, "error": exc.__class__.__name__}


def _migration_health(session: Session) -> dict:
    try:
        versions = session.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
    except Exception:
        versions = []
    return {"applied_versions": versions}
