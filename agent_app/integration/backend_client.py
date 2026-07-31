from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from typing import TypeVar

import httpx
from pydantic import ValidationError

from shared.backend_v13_contracts import (
    CommonErrorResponse,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)
from shared.settings import get_settings

ResponseModel = TypeVar("ResponseModel", RecordChangeResponse, NotificationPolicyChangeResponse)
_BACKEND_REQUEST_ATTEMPT_COUNT: ContextVar[int] = ContextVar(
    "backend_request_attempt_count",
    default=1,
)


def reset_backend_request_attempt_count() -> None:
    _BACKEND_REQUEST_ATTEMPT_COUNT.set(1)


def backend_request_attempt_count() -> int:
    return max(1, _BACKEND_REQUEST_ATTEMPT_COUNT.get())


class BackendV13ClientError(RuntimeError):
    pass


class BackendV13ConfigurationError(BackendV13ClientError):
    pass


class BackendV13TransportError(BackendV13ClientError):
    pass


class BackendV13ResponseError(BackendV13ClientError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_response: CommonErrorResponse | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_response = error_response


class BackendV13Client:
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
    def from_settings(cls) -> BackendV13Client:
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
        response = await self._post(
            self.record_change_path,
            request.model_dump(mode="json"),
            RecordChangeResponse,
        )
        self._validate_record_correlation(request, response)
        return response

    async def change_notification_policy(
        self,
        request: NotificationPolicyChangeRequest,
    ) -> NotificationPolicyChangeResponse:
        response = await self._post(
            self.notification_policy_change_path,
            request.model_dump(mode="json"),
            NotificationPolicyChangeResponse,
        )
        self._validate_policy_correlation(request, response)
        return response

    async def _post(self, path: str, body: dict, response_model: type[ResponseModel]) -> ResponseModel:
        if not self.base_url:
            raise BackendV13ConfigurationError("backend_base_url_required")
        if not path:
            raise BackendV13ConfigurationError("backend_api_path_required")
        if not self.bearer_token:
            raise BackendV13ConfigurationError("backend_api_token_required")

        headers = {"Authorization": self._authorization_value()}
        attempts = self.max_retries + 1
        last_transport_error: Exception | None = None
        reset_backend_request_attempt_count()

        async with httpx.AsyncClient(
            timeout=self.timeout_seconds,
            trust_env=False,
            transport=self.transport,
        ) as client:
            for attempt in range(attempts):
                _BACKEND_REQUEST_ATTEMPT_COUNT.set(attempt + 1)
                try:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=body,
                        headers=headers,
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    last_transport_error = exc
                    if attempt >= self.max_retries:
                        raise BackendV13TransportError("backend_request_failed_after_retries") from exc
                    await self.sleep(self._retry_delay(attempt))
                    continue

                if response.status_code in self.RETRYABLE_HTTP_STATUSES and attempt < self.max_retries:
                    await self.sleep(self._retry_delay(attempt))
                    continue
                if (
                    response.status_code == 409
                    and attempt < self.max_retries
                    and self._is_request_in_progress(response)
                ):
                    await self.sleep(
                        self._retry_after_or_delay(response, attempt)
                    )
                    continue

                return self._parse_response(response, response_model)

        raise BackendV13TransportError("backend_request_failed_after_retries") from last_transport_error

    def _parse_response(self, response: httpx.Response, response_model: type[ResponseModel]) -> ResponseModel:
        try:
            payload = response.json()
        except ValueError as exc:
            raise BackendV13ResponseError(
                f"backend_response_not_json:{response.status_code}",
                status_code=response.status_code,
            ) from exc
        if 200 <= response.status_code < 300:
            try:
                return response_model.model_validate(payload)
            except ValidationError as exc:
                raise BackendV13ResponseError(
                    f"backend_success_contract_invalid:{response.status_code}",
                    status_code=response.status_code,
                ) from exc

        try:
            error_response = CommonErrorResponse.model_validate(payload)
        except ValidationError as exc:
            raise BackendV13ResponseError(
                f"backend_error_contract_invalid:{response.status_code}",
                status_code=response.status_code,
            ) from exc
        raise BackendV13ResponseError(
            error_response.error.code,
            status_code=response.status_code,
            error_response=error_response,
        )

    def _authorization_value(self) -> str:
        if self.bearer_token.lower().startswith("bearer "):
            return self.bearer_token
        return f"Bearer {self.bearer_token}"

    def _retry_delay(self, attempt: int) -> float:
        if not self.retry_delays_seconds:
            return 0.0
        return self.retry_delays_seconds[min(attempt, len(self.retry_delays_seconds) - 1)]

    def _retry_after_or_delay(
        self,
        response: httpx.Response,
        attempt: int,
    ) -> float:
        raw = str(response.headers.get("Retry-After") or "").strip()
        try:
            parsed = float(raw)
        except ValueError:
            return self._retry_delay(attempt)
        return max(0.0, min(parsed, 30.0))

    @staticmethod
    def _is_request_in_progress(response: httpx.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        error = (
            payload.get("error")
            if isinstance(payload, dict)
            and isinstance(payload.get("error"), dict)
            else {}
        )
        return (
            error.get("code") == "REQUEST_IN_PROGRESS"
            and error.get("retryable") is True
        )

    @staticmethod
    def _validate_record_correlation(
        request: RecordChangeRequest,
        response: RecordChangeResponse,
    ) -> None:
        if response.request_id != request.request_id:
            raise BackendV13ResponseError(
                "backend_response_request_id_mismatch"
            )
        result = response.result
        if (
            result.resource_type != request.resource_type
            or result.operation != request.operation
        ):
            raise BackendV13ResponseError(
                "backend_response_record_operation_mismatch"
            )
        if request.record_id is not None:
            _validate_external_id_correlation(
                request.record_id,
                result.record_id,
                "backend_response_record_id_mismatch",
            )
        _validate_external_id_correlation(
            request.parent_record_id,
            result.parent_record_id,
            "backend_response_parent_record_id_mismatch",
        )

    @staticmethod
    def _validate_policy_correlation(
        request: NotificationPolicyChangeRequest,
        response: NotificationPolicyChangeResponse,
    ) -> None:
        if response.request_id != request.request_id:
            raise BackendV13ResponseError(
                "backend_response_request_id_mismatch"
            )
        result = response.result
        if (
            result.policy_id != request.policy_id
            or result.decision != request.payload.decision
        ):
            raise BackendV13ResponseError(
                "backend_response_policy_target_mismatch"
            )

    @staticmethod
    def _normalized_path(value: str) -> str:
        path = value.strip()
        if not path:
            return ""
        return path if path.startswith("/") else f"/{path}"


def _validate_external_id_correlation(
    requested_id: str | None,
    response_id: str | None,
    error_code: str,
) -> None:
    if requested_id is None:
        if response_id is not None:
            raise BackendV13ResponseError(error_code)
        return
    if response_id != requested_id:
        raise BackendV13ResponseError(error_code)
