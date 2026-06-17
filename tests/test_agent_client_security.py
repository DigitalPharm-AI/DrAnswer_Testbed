from __future__ import annotations

import asyncio

import httpx

from shared.settings import get_settings
from system_app.services.agent_client import AgentClient


class DummyHttpxResponse:
    status_code = 200

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {}


def test_agent_client_sends_internal_api_token(monkeypatch):
    captured: dict[str, object] = {}

    class DummyAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            self.timeout = timeout

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
