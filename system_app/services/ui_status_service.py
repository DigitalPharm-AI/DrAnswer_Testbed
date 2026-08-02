from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.orm import Session

from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from shared.redaction import stable_hash
from shared.settings import get_settings

AGENT_READINESS_TIMEOUT_SECONDS = 8.0


class _AgentReadinessComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: str
    dialect: str | None = None
    server_version: str | None = None
    migration_version: str | None = None
    symptom_count: int | None = None
    other_question_count: int | None = None


class _AgentReadinessComponents(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_database: _AgentReadinessComponent
    backend_read_database: _AgentReadinessComponent
    agent_sync_auth: _AgentReadinessComponent
    backend_write_api: _AgentReadinessComponent
    feedback_encryption: _AgentReadinessComponent
    pro_ctcae_reference: _AgentReadinessComponent
    generation_provider: _AgentReadinessComponent


class _AgentReadinessPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "not_ready"]
    contract_version: str
    components: _AgentReadinessComponents


@dataclass(frozen=True)
class _ServiceProbe:
    status: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class _CachedAgentReadiness:
    cache_key: tuple[str, str, int]
    probe: _ServiceProbe
    checked_at: str
    expires_at_monotonic: float


_agent_readiness_cache: _CachedAgentReadiness | None = None
_agent_readiness_cache_lock: asyncio.Lock | None = None
_agent_readiness_cache_loop: asyncio.AbstractEventLoop | None = None


async def collect_ui_service_status(
    session: Session,
    *,
    request_round_trip: bool = False,
) -> dict[str, object]:
    """Return a secret-free status summary for the React testbed."""

    settings = get_settings()
    cached_ai_probe = await _cached_agent_readiness(
        settings.agent_base_url,
        (settings.agent_sync_api_token or "").strip(),
        ttl_seconds=settings.ui_agent_status_cache_ttl_seconds,
    )
    # Probe the Backend DB after the potentially slow Agent readiness call so
    # the SQLAlchemy Session does not keep a checked-out DB connection while
    # waiting for the provider probe.
    backend_probe = _probe_backend_status(
        session,
        request_round_trip=request_round_trip,
    )
    backend_checked_at = datetime.now(UTC).isoformat()
    return {
        "backend_server": {
            "status": backend_probe.status,
            "checked_at": backend_checked_at,
            "evidence": list(backend_probe.evidence),
        },
        "ai_server": {
            "status": cached_ai_probe.probe.status,
            "checked_at": cached_ai_probe.checked_at,
            "evidence": list(cached_ai_probe.probe.evidence),
        },
    }


async def _cached_agent_readiness(
    base_url: str,
    agent_sync_token: str,
    *,
    ttl_seconds: float,
) -> _CachedAgentReadiness:
    global _agent_readiness_cache

    ttl = max(0.0, float(ttl_seconds))
    normalized_url = base_url.rstrip("/")
    cache_key = (
        normalized_url,
        stable_hash(agent_sync_token),
        id(_probe_agent_readiness),
    )
    now = monotonic()
    cached = _agent_readiness_cache
    if (
        ttl > 0
        and cached is not None
        and cached.cache_key == cache_key
        and cached.expires_at_monotonic > now
    ):
        return cached

    async with _agent_readiness_lock():
        now = monotonic()
        cached = _agent_readiness_cache
        if (
            ttl > 0
            and cached is not None
            and cached.cache_key == cache_key
            and cached.expires_at_monotonic > now
        ):
            return cached

        probe = await _probe_agent_readiness(
            normalized_url,
            agent_sync_token,
        )
        checked_at = datetime.now(UTC).isoformat()
        cached = _CachedAgentReadiness(
            cache_key=cache_key,
            probe=probe,
            checked_at=checked_at,
            expires_at_monotonic=monotonic() + ttl,
        )
        if ttl > 0:
            _agent_readiness_cache = cached
        return cached


def _agent_readiness_lock() -> asyncio.Lock:
    global _agent_readiness_cache_lock
    global _agent_readiness_cache_loop

    loop = asyncio.get_running_loop()
    if (
        _agent_readiness_cache_lock is None
        or _agent_readiness_cache_loop is not loop
    ):
        _agent_readiness_cache_lock = asyncio.Lock()
        _agent_readiness_cache_loop = loop
    return _agent_readiness_cache_lock


def reset_agent_readiness_cache() -> None:
    """Clear process-local status state for deterministic tests."""

    global _agent_readiness_cache
    _agent_readiness_cache = None


def _probe_backend_status(
    session: Session,
    *,
    request_round_trip: bool,
) -> _ServiceProbe:
    if not request_round_trip:
        return _ServiceProbe(
            status="failed",
            evidence=("REQUEST_ROUND_TRIP_NOT_CONFIRMED",),
        )

    try:
        result = session.execute(text("SELECT 1")).scalar_one()
    except (TimeoutError, SqlAlchemyTimeoutError):
        with contextlib.suppress(Exception):
            session.rollback()
        return _ServiceProbe(
            status="timeout",
            evidence=(
                "REQUEST_ROUND_TRIP_OK",
                "DATABASE_PROBE_TIMEOUT",
            ),
        )
    except Exception:
        with contextlib.suppress(Exception):
            session.rollback()
        return _ServiceProbe(
            status="failed",
            evidence=(
                "REQUEST_ROUND_TRIP_OK",
                "DATABASE_PROBE_FAILED",
            ),
        )

    if result != 1:
        return _ServiceProbe(
            status="incompatible",
            evidence=(
                "REQUEST_ROUND_TRIP_OK",
                "DATABASE_PROBE_INVALID_RESULT",
            ),
        )
    return _ServiceProbe(
        status="ready",
        evidence=(
            "REQUEST_ROUND_TRIP_OK",
            "DATABASE_PROBE_OK",
        ),
    )


async def _probe_agent_readiness(
    base_url: str,
    agent_sync_token: str,
) -> _ServiceProbe:
    if not agent_sync_token:
        return _ServiceProbe(
            status="failed",
            evidence=("AGENT_GENERATION_PROBE_AUTH_MISSING",),
        )
    url = f"{base_url.rstrip('/')}/health/generation/ready"
    try:
        async with httpx.AsyncClient(
            timeout=AGENT_READINESS_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {agent_sync_token}",
                },
            )
    except httpx.TimeoutException:
        return _ServiceProbe(
            status="timeout",
            evidence=("AGENT_READINESS_TIMEOUT",),
        )
    except httpx.TransportError:
        return _ServiceProbe(
            status="unreachable",
            evidence=("AGENT_READINESS_UNREACHABLE",),
        )
    except (httpx.InvalidURL, ValueError):
        return _ServiceProbe(
            status="incompatible",
            evidence=("AGENT_READINESS_URL_INVALID",),
        )
    except httpx.HTTPError:
        return _ServiceProbe(
            status="failed",
            evidence=("AGENT_READINESS_HTTP_FAILED",),
        )

    if response.status_code in {408, 504}:
        return _ServiceProbe(
            status="timeout",
            evidence=(f"AGENT_READINESS_HTTP_{response.status_code}",),
        )
    if response.status_code >= 500 and response.status_code != 503:
        return _ServiceProbe(
            status="failed",
            evidence=(f"AGENT_READINESS_HTTP_{response.status_code}",),
        )
    if response.status_code not in {200, 503}:
        return _ServiceProbe(
            status="incompatible",
            evidence=(
                f"AGENT_READINESS_HTTP_{response.status_code}",
                "AGENT_READINESS_HTTP_INCOMPATIBLE",
            ),
        )
    try:
        payload = _AgentReadinessPayload.model_validate(response.json())
    except (ValueError, TypeError):
        return _ServiceProbe(
            status="incompatible",
            evidence=("AGENT_READINESS_SCHEMA_INCOMPATIBLE",),
        )
    if payload.contract_version != BACKEND_READ_CONTRACT_VERSION:
        return _ServiceProbe(
            status="incompatible",
            evidence=("AGENT_READINESS_CONTRACT_INCOMPATIBLE",),
        )

    contract_evidence = (
        "AGENT_READINESS_CONTRACT_"
        + BACKEND_READ_CONTRACT_VERSION.replace(".", "_")
    )

    components = (
        ("agent_database", payload.components.agent_database),
        ("backend_read_database", payload.components.backend_read_database),
        ("agent_sync_auth", payload.components.agent_sync_auth),
        ("backend_write_api", payload.components.backend_write_api),
        ("feedback_encryption", payload.components.feedback_encryption),
        ("pro_ctcae_reference", payload.components.pro_ctcae_reference),
        ("generation_provider", payload.components.generation_provider),
    )
    failed_codes = tuple(
        component.code
        if component.code != "OK"
        else f"{name.upper()}_NOT_READY"
        for name, component in components
        if not component.ok
    )
    all_components_ready = not failed_codes
    generative_provider_ready = (
        payload.components.generation_provider.ok
        and payload.components.generation_provider.code == "OK"
    )

    if (
        response.status_code == 200
        and payload.status == "ready"
        and all_components_ready
        and generative_provider_ready
    ):
        return _ServiceProbe(
            status="ready",
            evidence=(
                "AGENT_READINESS_HTTP_OK",
                contract_evidence,
                "GENERATION_PROVIDER_OK",
            ),
        )
    if (
        response.status_code == 503
        and payload.status == "not_ready"
        and not all_components_ready
    ):
        return _ServiceProbe(
            status="not_ready",
            evidence=(
                "AGENT_READINESS_HTTP_503",
                contract_evidence,
                *failed_codes,
            ),
        )
    return _ServiceProbe(
        status="incompatible",
        evidence=("AGENT_READINESS_SEMANTICS_INCOMPATIBLE",),
    )
