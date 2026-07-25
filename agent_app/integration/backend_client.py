from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

import httpx
from pydantic import ValidationError

from agent_app.integration.contracts import (
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)
from shared.settings import get_settings

ResponseModel = TypeVar("ResponseModel", RecordChangeResponse, NotificationPolicyChangeResponse)


class BackendV12ClientError(RuntimeError):
    pass


class BackendV12ConfigurationError(BackendV12ClientError):
    pass


class BackendV12TransportError(BackendV12ClientError):
    pass


class BackendV12ResponseError(BackendV12ClientError):
    pass


class BackendV12Client:
    RETRYABLE_HTTP_STATUSES = frozenset({503, 504})

    def __init__(
        self,
        *,
        base_url: str,
        record_change_path: str,
        notification_policy_change_path: str,
        bearer_token: str,
        timeout_seconds: float = 90.0,
        max_retries: int = 2,
        retry_delays_seconds: tuple[float, ...] = (1.0, 2.0),
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.record_change_path = self._normalized_path(record_change_path)
        self.notification_policy_change_path = self._normalized_path(notification_policy_change_path)
        self.bearer_token = bearer_token.strip()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max(0, max_retries)
        self.retry_delays_seconds = retry_delays_seconds
        self.transport = transport
        self.sleep = sleep

    @classmethod
    def from_settings(cls) -> BackendV12Client:
        settings = get_settings()
        return cls(
            base_url=settings.system_base_url,
            record_change_path=settings.backend_record_change_path,
            notification_policy_change_path=settings.backend_notification_policy_change_path,
            bearer_token=settings.backend_api_token or "",
            timeout_seconds=settings.backend_api_timeout_seconds,
            max_retries=settings.backend_api_max_retries,
        )

    async def change_record(self, request: RecordChangeRequest) -> RecordChangeResponse:
        return await self._post(
            self.record_change_path,
            request.model_dump(mode="json"),
            RecordChangeResponse,
        )

    async def change_notification_policy(
        self,
        request: NotificationPolicyChangeRequest,
    ) -> NotificationPolicyChangeResponse:
        return await self._post(
            self.notification_policy_change_path,
            request.model_dump(mode="json"),
            NotificationPolicyChangeResponse,
        )

    async def _post(self, path: str, body: dict, response_model: type[ResponseModel]) -> ResponseModel:
        if not self.base_url:
            raise BackendV12ConfigurationError("backend_base_url_required")
        if not path:
            raise BackendV12ConfigurationError("backend_api_path_required")
        if not self.bearer_token:
            raise BackendV12ConfigurationError("backend_api_token_required")

        headers = {"Authorization": self._authorization_value()}
        attempts = self.max_retries + 1
        last_transport_error: Exception | None = None

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
            transport=self.transport,
        ) as client:
            for attempt in range(attempts):
                try:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=body,
                        headers=headers,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_transport_error = exc
                    if attempt >= self.max_retries:
                        raise BackendV12TransportError("backend_request_failed_after_retries") from exc
                    await self.sleep(self._retry_delay(attempt))
                    continue

                if response.status_code in self.RETRYABLE_HTTP_STATUSES and attempt < self.max_retries:
                    await self.sleep(self._retry_delay(attempt))
                    continue

                return self._parse_response(response, response_model)

        raise BackendV12TransportError("backend_request_failed_after_retries") from last_transport_error

    def _parse_response(self, response: httpx.Response, response_model: type[ResponseModel]) -> ResponseModel:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendV12ResponseError(f"backend_response_not_json:{response.status_code}") from exc
        try:
            parsed = response_model.model_validate(payload)
        except ValidationError as exc:
            raise BackendV12ResponseError(f"backend_response_contract_invalid:{response.status_code}") from exc

        if response.status_code >= 400 and parsed.success:
            raise BackendV12ResponseError(f"backend_error_status_with_success_body:{response.status_code}")
        if response.status_code < 400 and not parsed.success:
            raise BackendV12ResponseError(f"backend_success_status_with_error_body:{response.status_code}")
        return parsed

    def _authorization_value(self) -> str:
        if self.bearer_token.lower().startswith("bearer "):
            return self.bearer_token
        return f"Bearer {self.bearer_token}"

    def _retry_delay(self, attempt: int) -> float:
        if not self.retry_delays_seconds:
            return 0.0
        return self.retry_delays_seconds[min(attempt, len(self.retry_delays_seconds) - 1)]

    @staticmethod
    def _normalized_path(value: str) -> str:
        path = value.strip()
        if not path:
            return ""
        return path if path.startswith("/") else f"/{path}"
