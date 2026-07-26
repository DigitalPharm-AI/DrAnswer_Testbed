from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from shared.settings import get_settings
from shared.chat_contracts import (
    ChatMessageContent,
    ChatSyncRequest,
    ChatSyncResponse,
)
from system_app.services.agent_client import AgentClient, AgentServiceError


class DummyHttpxResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {}


def test_agent_client_sends_internal_api_token(monkeypatch):
    captured: dict[str, object] = {}

    class DummyAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool = True) -> None:
            self.timeout = timeout
            captured["trust_env"] = trust_env

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def request(self, method: str, url: str, *, json=None, headers=None):
            captured.update({"method": method, "url": url, "json": json, "headers": headers, "timeout": self.timeout})
            return DummyHttpxResponse()

    monkeypatch.setenv("INTERNAL_API_TOKEN", "agent-client-token")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "AsyncClient", DummyAsyncClient)

    try:
        asyncio.run(AgentClient("http://agent.test")._request_json("GET", "/agent/model-config", timeout=3.0))
    finally:
        get_settings.cache_clear()

    assert captured["url"] == "http://agent.test/agent/model-config"
    assert captured["headers"] == {"X-Internal-Api-Token": "agent-client-token"}
    assert captured["timeout"] == 3.0
    assert captured["trust_env"] is False


def test_agent_client_uses_only_dedicated_sync_token(monkeypatch):
    monkeypatch.setenv("AGENT_SYNC_API_TOKEN", "agent-sync-token")
    monkeypatch.setenv("BACKEND_API_TOKEN", "backend-write-token")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "legacy-internal-token")
    get_settings.cache_clear()

    try:
        client = AgentClient("http://agent.test")
        assert client._sync_headers() == {
            "Authorization": "Bearer agent-sync-token",
        }
    finally:
        get_settings.cache_clear()


def test_agent_client_rejects_missing_dedicated_sync_token(monkeypatch):
    monkeypatch.setenv("AGENT_SYNC_API_TOKEN", "")
    monkeypatch.setenv("BACKEND_API_TOKEN", "backend-write-token")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "legacy-internal-token")
    get_settings.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="AGENT_SYNC_API_TOKEN"):
            AgentClient("http://agent.test")
    finally:
        get_settings.cache_clear()


def test_agent_client_rejects_mismatched_sync_response_identifiers() -> None:
    request = ChatSyncRequest(
        request_id="request-001",
        message_id="message-001",
        conversation_id="conversation-001",
        patient_id="patient-001",
        requested_return_type=None,
        message="안녕하세요",
        message_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    )
    response = ChatSyncResponse(
        request_id="request-from-another-call",
        message_id="message-001",
        message_type="text",
        message=ChatMessageContent(
            message_title=None,
            text="응답",
            tables=None,
            selections=None,
            inputs=None,
        ),
        message_at=datetime(2026, 7, 25, 10, 1, tzinfo=UTC),
    )

    with pytest.raises(
        AgentServiceError,
        match="응답 식별자가 요청과 일치",
    ) as exc_info:
        AgentClient._validate_sync_chat_correlation(request, response)

    assert exc_info.value.error_type == "agent_response_correlation_mismatch"
    assert exc_info.value.retryable is False
