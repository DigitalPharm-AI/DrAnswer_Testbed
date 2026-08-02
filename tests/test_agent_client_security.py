from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from shared.chat_contracts import (
    ChatMessageContent,
    ChatSyncRequest,
    ChatSyncResponse,
)
from shared.settings import get_settings
from system_app.services import agent_client as agent_client_module
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


def test_agent_client_uses_shared_service_token(monkeypatch):
    monkeypatch.setenv("AGENT_SYNC_API_TOKEN", "agent-sync-token")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-admin-token")
    get_settings.cache_clear()

    try:
        client = AgentClient("http://agent.test")
        assert client._sync_headers() == {
            "Authorization": "Bearer agent-sync-token",
        }
    finally:
        get_settings.cache_clear()


def test_agent_client_rejects_missing_service_token(monkeypatch):
    monkeypatch.setenv("AGENT_SYNC_API_TOKEN", "")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "internal-admin-token")
    get_settings.cache_clear()

    try:
        with pytest.raises(RuntimeError, match="AGENT_SYNC_API_TOKEN"):
            AgentClient("http://agent.test")
    finally:
        get_settings.cache_clear()


def test_agent_client_rejects_mismatched_sync_response_identifiers() -> None:
    request = ChatSyncRequest(
        request_id="req_0000000000000001",
        message_id="user_msg_0000000000000001",
        patient_id="patient_0000000000000001",
        requested_return_type="text",
        message="안녕하세요",
        message_at=datetime(2026, 7, 25, 10, 0, tzinfo=UTC),
    )
    response = ChatSyncResponse(
        request_id="req_0000000000000002",
        message_id="user_msg_0000000000000001",
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


def test_sync_chat_retries_stay_inside_total_timeout_budget(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            "agent_sync_chat_timeout_seconds": 90.0,
            # model_copy bypasses Settings validation to exercise the
            # AgentClient's defensive runtime clamp.
            "agent_sync_chat_total_timeout_seconds": 500.0,
            "agent_sync_max_retries": 2,
        }
    )
    clock = 0.0
    attempted_timeouts: list[float] = []
    wall_clock_timeouts: list[float] = []
    retry_delays: list[float] = []

    def fake_monotonic() -> float:
        return clock

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        retry_delays.append(seconds)
        clock += seconds

    class CapturingTimeout:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    def fake_timeout(seconds: float) -> CapturingTimeout:
        wall_clock_timeouts.append(seconds)
        return CapturingTimeout()

    class TimeoutAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert trust_env is False
            self.timeout = timeout
            attempted_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method: str, url: str, **_kwargs):
            timeout = self.timeout

            class _TimeoutStream:
                async def __aenter__(self):
                    nonlocal clock
                    clock += timeout
                    raise httpx.ReadTimeout(
                        "simulated timeout",
                        request=httpx.Request("POST", url),
                    )

                async def __aexit__(self, *_args):
                    return None

            return _TimeoutStream()

    monkeypatch.setattr(agent_client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        agent_client_module.time,
        "monotonic",
        fake_monotonic,
    )
    monkeypatch.setattr(agent_client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent_client_module.asyncio, "timeout", fake_timeout)
    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        TimeoutAsyncClient,
    )
    request = ChatSyncRequest(
        request_id="req_0000000000000003",
        message_id="user_msg_0000000000000003",
        patient_id="patient_0000000000000003",
        requested_return_type="text",
        message="시간 예산을 확인해 주세요.",
        message_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    with pytest.raises(AgentServiceError) as exc_info:
        asyncio.run(
            AgentClient("http://agent.test").send_sync_chat(request)
        )

    assert exc_info.value.retryable is True
    assert attempted_timeouts == pytest.approx([95.0, 4.0])
    assert wall_clock_timeouts == pytest.approx(attempted_timeouts)
    assert retry_delays == [1.0]
    assert clock == pytest.approx(100.0)


def test_sync_chat_wall_clock_timeout_is_reported_as_retryable(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            "agent_sync_chat_total_timeout_seconds": 0.1,
            "agent_sync_max_retries": 0,
        }
    )

    class SlowAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert timeout <= 0.101
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, _method: str, _url: str, **_kwargs):
            class _SlowStream:
                async def __aenter__(self):
                    await asyncio.sleep(1.0)
                    raise AssertionError(
                        "wall-clock timeout did not cancel the call"
                    )

                async def __aexit__(self, *_args):
                    return None

            return _SlowStream()

    monkeypatch.setattr(agent_client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        SlowAsyncClient,
    )
    request = ChatSyncRequest(
        request_id="req_0000000000000004",
        message_id="user_msg_0000000000000004",
        patient_id="patient_0000000000000004",
        requested_return_type="text",
        message="전체 경과 시간 제한을 확인해 주세요.",
        message_at=datetime(2026, 7, 28, 10, 0, tzinfo=UTC),
    )

    with pytest.raises(AgentServiceError) as exc_info:
        asyncio.run(
            AgentClient("http://agent.test").send_sync_chat(request)
        )

    assert exc_info.value.error_type == "agent_network_error"
    assert exc_info.value.retryable is True


def test_feedback_fast_503_responses_retry_inside_total_budget(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            "agent_sync_feedback_total_timeout_seconds": 15.0,
            "agent_sync_max_retries": 2,
        }
    )
    clock = 0.0
    calls = 0
    attempted_timeouts: list[float] = []
    retry_delays: list[float] = []

    def fake_monotonic() -> float:
        return clock

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        retry_delays.append(seconds)
        clock += seconds

    class FeedbackAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert trust_env is False
            attempted_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, **_kwargs):
            nonlocal calls
            calls += 1
            return httpx.Response(
                503 if calls < 3 else 202,
                json=(
                    {
                        "success": False,
                        "data": None,
                        "error": {
                            "code": "AGENT_BUSY",
                            "message": "busy",
                            "retryable": True,
                            "details": None,
                        },
                    }
                    if calls < 3
                    else {"status": "accepted"}
                ),
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(agent_client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        agent_client_module.time,
        "monotonic",
        fake_monotonic,
    )
    monkeypatch.setattr(agent_client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        FeedbackAsyncClient,
    )

    result = asyncio.run(
        AgentClient("http://agent.test").send_chat_feedback(
            {"request_id": "feedback-budget"}
        )
    )

    assert result == {
        "status": "accepted",
        "reaction": None,
        "accepted_at": None,
    }
    assert calls == 3
    assert retry_delays == [1.0, 2.0]
    assert attempted_timeouts == [10.0, 10.0, 10.0]
    assert clock == 3.0


def test_feedback_timeouts_stay_inside_total_timeout_budget(
    monkeypatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            # The effective client budget must remain below the browser's
            # 20-second feedback timeout even after a bad runtime override.
            "agent_sync_feedback_total_timeout_seconds": 60.0,
            "agent_sync_max_retries": 2,
        }
    )
    clock = 0.0
    attempted_timeouts: list[float] = []
    wall_clock_timeouts: list[float] = []
    retry_delays: list[float] = []

    def fake_monotonic() -> float:
        return clock

    async def fake_sleep(seconds: float) -> None:
        nonlocal clock
        retry_delays.append(seconds)
        clock += seconds

    class CapturingTimeout:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_args):
            return None

    def fake_timeout(seconds: float) -> CapturingTimeout:
        wall_clock_timeouts.append(seconds)
        return CapturingTimeout()

    class TimeoutAsyncClient:
        def __init__(self, *, timeout: float, trust_env: bool) -> None:
            assert trust_env is False
            self.timeout = timeout
            attempted_timeouts.append(timeout)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, **_kwargs):
            nonlocal clock
            clock += self.timeout
            raise httpx.ReadTimeout(
                "simulated timeout",
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(agent_client_module, "get_settings", lambda: settings)
    monkeypatch.setattr(
        agent_client_module.time,
        "monotonic",
        fake_monotonic,
    )
    monkeypatch.setattr(agent_client_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent_client_module.asyncio, "timeout", fake_timeout)
    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        TimeoutAsyncClient,
    )

    with pytest.raises(AgentServiceError) as exc_info:
        asyncio.run(
            AgentClient("http://agent.test").send_chat_feedback(
                {"request_id": "feedback-timeout-budget"}
            )
        )

    assert exc_info.value.retryable is True
    assert attempted_timeouts == pytest.approx([10.0, 4.0])
    assert wall_clock_timeouts == pytest.approx(attempted_timeouts)
    assert retry_delays == [1.0]
    assert clock == pytest.approx(15.0)
