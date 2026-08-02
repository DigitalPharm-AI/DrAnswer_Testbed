from __future__ import annotations

import asyncio
from unittest.mock import Mock

import httpx
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from shared.backend_read_contract import BACKEND_READ_CONTRACT_VERSION
from system_app.services import ui_status_service


def _readiness_payload(
    status: str,
    *,
    generation_ok: bool = True,
    generation_code: str = "OK",
    contract_version: str = BACKEND_READ_CONTRACT_VERSION,
) -> dict:
    component = {"ok": status == "ready", "code": "OK"}
    return {
        "status": status,
        "contract_version": contract_version,
        "components": {
            "agent_database": component,
            "backend_read_database": component,
            "agent_sync_auth": component,
            "backend_write_api": component,
            "feedback_encryption": component,
            "pro_ctcae_reference": {
                "ok": status == "ready",
                "code": "OK",
                "symptom_count": 80,
                "other_question_count": 7,
            },
            "generation_provider": {
                "ok": generation_ok,
                "code": generation_code,
            },
        },
    }


def _fake_async_client(monkeypatch, result):
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            return False

        async def get(self, url: str, *, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            if isinstance(result, Exception):
                raise result
            return result

    monkeypatch.setattr(
        ui_status_service.httpx,
        "AsyncClient",
        FakeAsyncClient,
    )
    return captured


def test_backend_probe_requires_request_round_trip_and_database_success() -> None:
    session = Mock()
    session.execute.return_value.scalar_one.return_value = 1

    no_round_trip = ui_status_service._probe_backend_status(
        session,
        request_round_trip=False,
    )

    assert no_round_trip.status == "failed"
    assert no_round_trip.evidence == ("REQUEST_ROUND_TRIP_NOT_CONFIRMED",)
    session.execute.assert_not_called()

    ready = ui_status_service._probe_backend_status(
        session,
        request_round_trip=True,
    )

    assert ready.status == "ready"
    assert ready.evidence == (
        "REQUEST_ROUND_TRIP_OK",
        "DATABASE_PROBE_OK",
    )
    session.execute.assert_called_once()


def test_backend_probe_reports_failure_timeout_and_invalid_result() -> None:
    failed_session = Mock()
    failed_session.execute.side_effect = RuntimeError("private-db-error")
    failed = ui_status_service._probe_backend_status(
        failed_session,
        request_round_trip=True,
    )
    assert failed.status == "failed"
    assert failed.evidence == (
        "REQUEST_ROUND_TRIP_OK",
        "DATABASE_PROBE_FAILED",
    )
    failed_session.rollback.assert_called_once()
    assert "private-db-error" not in str(failed)

    timeout_session = Mock()
    timeout_session.execute.side_effect = SqlAlchemyTimeoutError("pool-timeout")
    timed_out = ui_status_service._probe_backend_status(
        timeout_session,
        request_round_trip=True,
    )
    assert timed_out.status == "timeout"
    assert timed_out.evidence == (
        "REQUEST_ROUND_TRIP_OK",
        "DATABASE_PROBE_TIMEOUT",
    )
    timeout_session.rollback.assert_called_once()

    invalid_session = Mock()
    invalid_session.execute.return_value.scalar_one.return_value = 0
    invalid = ui_status_service._probe_backend_status(
        invalid_session,
        request_round_trip=True,
    )
    assert invalid.status == "incompatible"
    assert invalid.evidence == (
        "REQUEST_ROUND_TRIP_OK",
        "DATABASE_PROBE_INVALID_RESULT",
    )


def test_ai_readiness_probe_requires_http_contract_and_generation_provider(
    monkeypatch,
) -> None:
    captured = _fake_async_client(
        monkeypatch,
        httpx.Response(200, json=_readiness_payload("ready")),
    )

    ready = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001/",
            "agent-sync-token",
        )
    )

    assert ready.status == "ready"
    assert ready.evidence == (
        "AGENT_READINESS_HTTP_OK",
        "AGENT_READINESS_CONTRACT_1_4",
        "GENERATION_PROVIDER_OK",
    )
    assert captured == {
        "timeout": 8.0,
        "trust_env": False,
        "url": "http://agent:8001/health/generation/ready",
        "headers": {
            "Authorization": "Bearer agent-sync-token",
        },
    }

    _fake_async_client(
        monkeypatch,
        httpx.Response(503, json=_readiness_payload("not_ready")),
    )
    not_ready = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert not_ready.status == "not_ready"


def test_ai_readiness_probe_accepts_declared_database_evidence(
    monkeypatch,
) -> None:
    payload = _readiness_payload("ready")
    payload["components"]["agent_database"].update(
        {
            "dialect": "postgresql",
            "server_version": "18.3",
            "migration_version": "20260727_0029_agent_async_task_claim_index",
        }
    )
    payload["components"]["backend_read_database"].update(
        {
            "dialect": "postgresql",
            "server_version": "18.3",
        }
    )
    _fake_async_client(
        monkeypatch,
        httpx.Response(200, json=payload),
    )

    result = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )

    assert result.status == "ready"
    assert "GENERATION_PROVIDER_OK" in result.evidence


def test_ai_readiness_probe_never_marks_failed_generation_provider_ready(
    monkeypatch,
) -> None:
    _fake_async_client(
        monkeypatch,
        httpx.Response(
            503,
            json=_readiness_payload(
                "not_ready",
                generation_ok=False,
                generation_code="GENERATION_PROVIDER_INVOCATION_FAILED",
            ),
        ),
    )

    result = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )

    assert result.status == "not_ready"
    assert "GENERATION_PROVIDER_INVOCATION_FAILED" in result.evidence


def test_ai_readiness_probe_distinguishes_incompatible_unreachable_and_timeout(
    monkeypatch,
) -> None:
    _fake_async_client(
        monkeypatch,
        httpx.Response(
            200,
            json=_readiness_payload(
                "ready",
                contract_version="1.3",
            ),
        ),
    )
    version_mismatch = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert version_mismatch.status == "incompatible"
    assert version_mismatch.evidence == (
        "AGENT_READINESS_CONTRACT_INCOMPATIBLE",
    )

    legacy_payload = _readiness_payload("ready")
    legacy_payload["components"].pop("generation_provider")
    _fake_async_client(
        monkeypatch,
        httpx.Response(
            200,
            json=legacy_payload,
        ),
    )
    incompatible = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert incompatible.status == "incompatible"
    assert incompatible.evidence == (
        "AGENT_READINESS_SCHEMA_INCOMPATIBLE",
    )

    request = httpx.Request("GET", "http://agent:8001/health/ready")
    _fake_async_client(
        monkeypatch,
        httpx.ConnectError("offline", request=request),
    )
    unreachable = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert unreachable.status == "unreachable"
    assert unreachable.evidence == ("AGENT_READINESS_UNREACHABLE",)

    _fake_async_client(
        monkeypatch,
        httpx.ReadTimeout("slow", request=request),
    )
    timed_out = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert timed_out.status == "timeout"
    assert timed_out.evidence == ("AGENT_READINESS_TIMEOUT",)

    _fake_async_client(
        monkeypatch,
        httpx.InvalidURL("invalid agent base URL"),
    )
    invalid_url = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "not-an-http-url",
            "agent-sync-token",
        )
    )
    assert invalid_url.status == "incompatible"
    assert invalid_url.evidence == ("AGENT_READINESS_URL_INVALID",)


def test_ai_readiness_probe_reports_http_failure_and_semantic_mismatch(
    monkeypatch,
) -> None:
    _fake_async_client(
        monkeypatch,
        httpx.Response(500, json={"status": "failed"}),
    )
    failed = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert failed.status == "failed"
    assert failed.evidence == ("AGENT_READINESS_HTTP_500",)

    _fake_async_client(
        monkeypatch,
        httpx.Response(
            200,
            json=_readiness_payload(
                "ready",
                generation_ok=False,
                generation_code="GENERATION_PROVIDER_INVOCATION_FAILED",
            ),
        ),
    )
    mismatch = asyncio.run(
        ui_status_service._probe_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
        )
    )
    assert mismatch.status == "incompatible"
    assert mismatch.evidence == (
        "AGENT_READINESS_SEMANTICS_INCOMPATIBLE",
    )


def test_ai_readiness_cache_reuses_probe_and_single_flights_concurrent_calls(
    monkeypatch,
) -> None:
    calls = 0

    async def counted_probe(_base_url: str, _agent_sync_token: str):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return ui_status_service._ServiceProbe(
            status="ready",
            evidence=("GENERATION_PROVIDER_OK",),
        )

    monkeypatch.setattr(
        ui_status_service,
        "_probe_agent_readiness",
        counted_probe,
    )
    ui_status_service.reset_agent_readiness_cache()

    async def scenario():
        first_batch = await asyncio.gather(
            *(
                ui_status_service._cached_agent_readiness(
                    "http://agent:8001/",
                    "agent-sync-token",
                    ttl_seconds=60,
                )
                for _ in range(8)
            )
        )
        uncached = await ui_status_service._cached_agent_readiness(
            "http://agent:8001",
            "agent-sync-token",
            ttl_seconds=0,
        )
        return first_batch, uncached

    try:
        first_batch, uncached = asyncio.run(scenario())
    finally:
        ui_status_service.reset_agent_readiness_cache()

    assert calls == 2
    assert len({item.checked_at for item in first_batch}) == 1
    assert all(item.probe.status == "ready" for item in first_batch)
    assert uncached.probe.status == "ready"
