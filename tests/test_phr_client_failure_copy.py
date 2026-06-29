from __future__ import annotations

import httpx
import pytest

from system_app.services.phr_client import PhrClient, PhrServiceError


class DummyAsyncClient:
    def __init__(self, *, timeout: float, **_kwargs) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def request(self, method: str, url: str, *, json=None):
        response = httpx.Response(
            503,
            json={"detail": "phr_read_only_mode"},
            request=httpx.Request(method, url),
        )
        response.raise_for_status()


class RawDetailDummyAsyncClient:
    def __init__(self, *, timeout: float, **_kwargs) -> None:
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def request(self, method: str, url: str, *, json=None):
        response = httpx.Response(
            500,
            json={"detail": "pytest private PHR upstream detail peanut allergy token=secret-value"},
            request=httpx.Request(method, url),
        )
        response.raise_for_status()


@pytest.mark.asyncio
async def test_phr_client_maps_read_only_status_to_user_safe_message(monkeypatch):
    monkeypatch.setattr("system_app.services.phr_client.httpx.AsyncClient", DummyAsyncClient)
    client = PhrClient(base_url="http://phr.test", timeout_seconds=1)

    with pytest.raises(PhrServiceError) as exc_info:
        await client.register_patient([])

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "phr_read_only_mode"
    assert "PHR 등록이 잠시 중지" in exc_info.value.message


@pytest.mark.asyncio
async def test_phr_client_does_not_preserve_unknown_upstream_detail(monkeypatch):
    monkeypatch.setattr("system_app.services.phr_client.httpx.AsyncClient", RawDetailDummyAsyncClient)
    client = PhrClient(base_url="http://phr.test", timeout_seconds=1)

    with pytest.raises(PhrServiceError) as exc_info:
        await client.register_patient([])

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "phr_upstream_error"
    assert "pytest private PHR upstream detail" not in exc_info.value.detail
    assert "secret-value" not in exc_info.value.detail
    assert "PHR 서버와 연결하지 못했습니다" in exc_info.value.message
