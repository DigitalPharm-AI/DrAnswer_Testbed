from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from shared.async_v13_contracts import (
    AsyncEventAccepted,
    DailyMedicationPatternAnalysisRequest,
    MissedDoseEventRequest,
)
from system_app.contracts_ui_feedback import AgentChatFeedbackAccepted
from shared.chat_contracts import (
    ChatStreamEvent,
    ChatSyncRequest,
    ChatSyncResponse,
)
from shared.schemas import (
    AgentAsyncClinicianAlertRequest,
    AgentInternalTaskAccepted,
    AgentModelConfig,
    AgentModelTierRequest,
)
from shared.settings import get_settings
from system_app.services.chat_stream import ui_text_publisher

MAX_CHAT_TOTAL_TIMEOUT_SECONDS = 100.0
MAX_FEEDBACK_TOTAL_TIMEOUT_SECONDS = 15.0
MAX_STREAM_EVENT_BYTES = 1_048_576


@dataclass
class _SyncChatAttemptState:
    public_delta_published: bool = False
    retryable_terminal_received: bool = False


def _remaining_budget_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


async def _wait_for_retry(deadline: float, delay_seconds: float) -> bool:
    """Wait only when the retry can start inside the caller's total budget."""

    if _remaining_budget_seconds(deadline) <= delay_seconds:
        return False
    await asyncio.sleep(delay_seconds)
    return _remaining_budget_seconds(deadline) > 0


class AgentServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str = "agent_service_error",
        agent_name: str | None = None,
        decision_type: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.agent_name = agent_name
        self.decision_type = decision_type
        self.retryable = retryable
        self.details = details


class AgentClient:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.agent_base_url).rstrip("/")
        self.internal_api_token = settings.require_internal_api_token()
        self.agent_sync_api_token = settings.require_agent_sync_api_token()
        self.llm_timeout_seconds = settings.llm_timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {"X-Internal-Api-Token": self.internal_api_token}

    def _sync_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.agent_sync_api_token}"}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float = 60.0,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    json=payload,
                    headers=headers if headers is not None else self._headers(),
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._build_service_error(exc.response) from exc
        except httpx.HTTPError as exc:
            raise AgentServiceError(
                "에이전트 서버와 통신하지 못했습니다.",
                error_type="agent_network_error",
            ) from exc
        try:
            result = response.json()
        except ValueError as exc:
            raise AgentServiceError(
                "에이전트 응답을 JSON으로 해석하지 못했습니다.",
                status_code=response.status_code,
                error_type="agent_response_invalid",
            ) from exc
        if not isinstance(result, dict):
            raise AgentServiceError(
                "에이전트 응답이 JSON 객체가 아닙니다.",
                status_code=response.status_code,
                error_type="agent_response_invalid",
            )
        return result

    @staticmethod
    def _validate_response_model(data: dict[str, Any], model_type: type[BaseModel], error_message: str):
        try:
            return model_type.model_validate(data)
        except ValidationError as exc:
            raise AgentServiceError(
                error_message,
                error_type="agent_response_invalid",
            ) from exc

    async def get_model_config(self) -> AgentModelConfig:
        data = await self._request_json("GET", "/agent/model-config", timeout=10.0)
        return self._validate_response_model(data, AgentModelConfig, "에이전트 모델 설정 응답을 해석하지 못했습니다.")

    async def set_model_tier(self, model_tier: str) -> AgentModelConfig:
        data = await self._request_json(
            "POST",
            "/agent/model-config",
            payload=AgentModelTierRequest(model_tier=model_tier).model_dump(mode="json"),
            timeout=10.0,
        )
        return self._validate_response_model(data, AgentModelConfig, "에이전트 모델 설정 응답을 해석하지 못했습니다.")

    @staticmethod
    def _build_service_error(response: httpx.Response) -> AgentServiceError:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        error = (
            payload.get("error")
            if isinstance(payload, dict)
            and isinstance(payload.get("error"), dict)
            else (
                payload
                if isinstance(payload, dict)
                and isinstance(payload.get("code"), str)
                else {}
            )
        )
        message = error.get("message") or (payload.get("message") if isinstance(payload, dict) else None)
        if not message:
            message = f"에이전트 서버가 {response.status_code} 오류를 반환했습니다."

        return AgentServiceError(
            message,
            status_code=response.status_code,
            error_type=error.get("code") or (payload.get("error_type", "agent_service_error") if isinstance(payload, dict) else "agent_service_error"),
            agent_name=payload.get("agent_name") if isinstance(payload, dict) else None,
            decision_type=payload.get("decision_type") if isinstance(payload, dict) else None,
            retryable=bool(error.get("retryable")),
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
        )

    async def send_sync_chat(self, payload: ChatSyncRequest) -> ChatSyncResponse:
        settings = get_settings()
        max_retries = max(0, settings.agent_sync_max_retries)
        per_attempt_timeout = max(
            1.0,
            float(settings.agent_sync_chat_timeout_seconds) + 5.0,
        )
        deadline = time.monotonic() + min(
            MAX_CHAT_TOTAL_TIMEOUT_SECONDS,
            max(
                0.1,
                float(settings.agent_sync_chat_total_timeout_seconds),
            ),
        )
        for attempt in range(max_retries + 1):
            remaining = _remaining_budget_seconds(deadline)
            if remaining <= 0:
                break
            timeout = min(per_attempt_timeout, remaining)
            attempt_state = _SyncChatAttemptState()
            try:
                async with asyncio.timeout(timeout):
                    return await self._send_sync_chat_ndjson_attempt(
                        payload,
                        timeout=timeout,
                        attempt_state=attempt_state,
                    )
            except AgentServiceError as exc:
                if (
                    exc.status_code == 200
                    and exc.retryable
                    and attempt_state.retryable_terminal_received
                    and not attempt_state.public_delta_published
                    and attempt < max_retries
                    and await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    )
                ):
                    continue
                if (
                    exc.status_code in {503, 504}
                    and not attempt_state.public_delta_published
                    and attempt < max_retries
                    and await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    )
                ):
                    continue
                if (
                    exc.error_type == "REQUEST_IN_PROGRESS"
                    and exc.retryable
                    and not attempt_state.public_delta_published
                    and attempt < max_retries
                    and await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    )
                ):
                    continue
                raise
            except (
                TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:
                if (
                    not attempt_state.public_delta_published
                    and attempt < max_retries
                ):
                    if await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    ):
                        continue
                raise AgentServiceError(
                    "에이전트 서버와 통신하지 못했습니다.",
                    error_type="agent_network_error",
                    retryable=True,
                ) from exc
        raise AgentServiceError(
            "에이전트 채팅의 전체 응답 시간 예산이 초과되었습니다.",
            error_type="agent_timeout_budget_exhausted",
            retryable=True,
        )

    async def _send_sync_chat_ndjson_attempt(
        self,
        payload: ChatSyncRequest,
        *,
        timeout: float,
        attempt_state: _SyncChatAttemptState,
    ) -> ChatSyncResponse:
        publisher = ui_text_publisher()
        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
        ) as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/agent/sync/chat",
                json=payload.model_dump(mode="json"),
                headers={
                    **self._sync_headers(),
                    "Accept": "application/x-ndjson",
                },
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._build_service_error(response)
                content_type = response.headers.get(
                    "content-type",
                    "",
                )
                if (
                    "application/x-ndjson"
                    not in content_type.lower()
                ):
                    raise AgentServiceError(
                        (
                            "에이전트 NDJSON 스트림의 Content-Type이 "
                            "올바르지 않습니다."
                        ),
                        status_code=response.status_code,
                        error_type="agent_stream_invalid",
                    )

                expected_sequence = 0
                completed: ChatSyncResponse | None = None
                terminal_error: AgentServiceError | None = None
                terminal_seen = False
                async for data in _iter_ndjson_objects(response):
                    if terminal_seen:
                        raise AgentServiceError(
                            (
                                "에이전트 종단 이벤트 뒤에 추가 "
                                "데이터가 도착했습니다."
                            ),
                            status_code=response.status_code,
                            error_type="agent_stream_invalid",
                        )
                    try:
                        event = ChatStreamEvent.model_validate(data)
                    except ValidationError as exc:
                        raise AgentServiceError(
                            (
                                "에이전트 NDJSON 이벤트 계약이 "
                                "올바르지 않습니다."
                            ),
                            status_code=response.status_code,
                            error_type="agent_stream_invalid",
                        ) from exc
                    _validate_stream_correlation(payload, event)
                    if event.sequence != expected_sequence:
                        raise AgentServiceError(
                            "에이전트 스트림 순서가 올바르지 않습니다.",
                            status_code=response.status_code,
                            error_type="agent_stream_invalid",
                        )
                    expected_sequence += 1

                    if event.status == "streaming":
                        delta = event.delta or ""
                        if delta:
                            attempt_state.public_delta_published = True
                        if publisher is not None:
                            await publisher(delta)
                        continue

                    terminal_seen = True
                    if event.status == "completed":
                        completed = ChatSyncResponse(
                            request_id=event.request_id,
                            message_id=event.message_id,
                            message_type=event.message_type,
                            message=event.message,
                            message_at=event.event_at,
                        )
                        self._validate_sync_chat_correlation(
                            payload,
                            completed,
                        )
                        continue

                    error = event.error
                    if error is None:
                        raise AgentServiceError(
                            "에이전트 오류 이벤트가 비어 있습니다.",
                            status_code=response.status_code,
                            error_type="agent_stream_invalid",
                        )
                    attempt_state.retryable_terminal_received = (
                        error.retryable
                    )
                    terminal_error = AgentServiceError(
                        error.message,
                        status_code=response.status_code,
                        error_type=error.code,
                        retryable=error.retryable,
                        details=error.details,
                    )

                if terminal_error is not None:
                    raise terminal_error
                if completed is not None:
                    return completed
                raise AgentServiceError(
                    (
                        "에이전트 NDJSON 스트림이 종단 이벤트 없이 "
                        "종료되었습니다."
                    ),
                    status_code=response.status_code,
                    error_type="agent_stream_incomplete",
                    retryable=True,
                )

    async def send_chat_feedback(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit a reaction and/or opinion over the authenticated boundary."""

        settings = get_settings()
        max_retries = max(0, settings.agent_sync_max_retries)
        deadline = time.monotonic() + min(
            MAX_FEEDBACK_TOTAL_TIMEOUT_SECONDS,
            max(
                0.1,
                float(settings.agent_sync_feedback_total_timeout_seconds),
            ),
        )
        for attempt in range(max_retries + 1):
            remaining = _remaining_budget_seconds(deadline)
            if remaining <= 0:
                break
            try:
                async with httpx.AsyncClient(
                    timeout=min(10.0, remaining),
                    trust_env=False,
                ) as client:
                    attempt_timeout = min(10.0, remaining)
                    async with asyncio.timeout(attempt_timeout):
                        response = await client.post(
                            f"{self.base_url}/agent/async/chat_feedback",
                            json=payload,
                            headers=self._sync_headers(),
                        )
                if (
                    response.status_code in {503, 504}
                    and attempt < max_retries
                ):
                    if await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    ):
                        continue
                response.raise_for_status()
                if response.status_code != 202:
                    raise AgentServiceError(
                        "The Agent feedback response did not use HTTP 202.",
                        status_code=response.status_code,
                        error_type="agent_response_status_invalid",
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgentServiceError(
                        "The Agent feedback response was not valid JSON.",
                        status_code=response.status_code,
                        error_type="agent_response_invalid",
                    ) from exc
                if not isinstance(data, dict):
                    raise AgentServiceError(
                        "The Agent feedback response was not a JSON object.",
                        status_code=response.status_code,
                        error_type="agent_response_invalid",
                    )
                try:
                    accepted = AgentChatFeedbackAccepted.model_validate(data)
                except ValidationError as exc:
                    raise AgentServiceError(
                        "The Agent feedback response violated the v1.3 contract.",
                        status_code=response.status_code,
                        error_type="agent_response_contract_invalid",
                    ) from exc
                return accepted.model_dump(mode="json")
            except httpx.HTTPStatusError as exc:
                raise self._build_service_error(exc.response) from exc
            except (
                TimeoutError,
                httpx.TimeoutException,
                httpx.TransportError,
            ) as exc:
                if attempt < max_retries:
                    if await _wait_for_retry(
                        deadline,
                        float(2**attempt),
                    ):
                        continue
                raise AgentServiceError(
                    "The Agent feedback boundary is unavailable.",
                    error_type="agent_network_error",
                    retryable=True,
                ) from exc
        raise AgentServiceError(
            "The Agent feedback total timeout budget was exhausted.",
            error_type="agent_timeout_budget_exhausted",
            retryable=True,
        )

    @staticmethod
    def _validate_sync_chat_correlation(
        request: ChatSyncRequest,
        response: ChatSyncResponse,
    ) -> None:
        if (
            response.request_id != request.request_id
            or response.message_id != request.message_id
        ):
            raise AgentServiceError(
                "에이전트 응답 식별자가 요청과 일치하지 않습니다.",
                error_type="agent_response_correlation_mismatch",
                retryable=False,
                details={
                    "expected_request_id": request.request_id,
                    "expected_message_id": request.message_id,
                },
            )

    async def send_medication_event_async(
        self,
        payload: MissedDoseEventRequest,
    ) -> AsyncEventAccepted:
        return await self._send_async_acceptance(
            "/agent/async/missed-dose-events",
            payload,
        )

    async def send_daily_pattern_analysis_async(
        self,
        payload: DailyMedicationPatternAnalysisRequest,
    ) -> AsyncEventAccepted:
        return await self._send_async_acceptance(
            "/agent/async/daily-medication-pattern-analysis",
            payload,
        )

    async def _send_async_acceptance(
        self,
        path: str,
        payload: MissedDoseEventRequest
        | DailyMedicationPatternAnalysisRequest,
    ) -> AsyncEventAccepted:
        settings = get_settings()
        max_retries = max(0, settings.agent_sync_max_retries)
        body = payload.model_dump(mode="json")
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=10.0,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self.base_url}{path}",
                        json=body,
                        headers=self._sync_headers(),
                    )
                if (
                    response.status_code in {503, 504}
                    and attempt < max_retries
                ):
                    await asyncio.sleep(float(2**attempt))
                    continue
                response.raise_for_status()
                if response.status_code not in {200, 202}:
                    raise AgentServiceError(
                        (
                            "에이전트 비동기 접수 응답이 HTTP 200 또는 "
                            "202를 사용하지 않았습니다."
                        ),
                        status_code=response.status_code,
                        error_type="agent_response_status_invalid",
                    )
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgentServiceError(
                        "에이전트 비동기 접수 응답이 올바른 JSON이 아닙니다.",
                        status_code=response.status_code,
                        error_type="agent_response_invalid",
                    ) from exc
                if not isinstance(data, dict):
                    raise AgentServiceError(
                        "에이전트 비동기 접수 응답이 JSON 객체가 아닙니다.",
                        status_code=response.status_code,
                        error_type="agent_response_invalid",
                    )
                break
            except httpx.HTTPStatusError as exc:
                raise self._build_service_error(exc.response) from exc
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(float(2**attempt))
                    continue
                raise AgentServiceError(
                    "에이전트 서버와 통신하지 못했습니다.",
                    error_type="agent_network_error",
                    retryable=True,
                ) from exc
        else:  # pragma: no cover - loop always returns or raises
            raise RuntimeError("unreachable_async_accept_retry_state")
        accepted = self._validate_response_model(
            data,
            AsyncEventAccepted,
            "에이전트 비동기 접수 응답을 해석하지 못했습니다.",
        )
        if (
            accepted.request_id != payload.request_id
        ):
            raise AgentServiceError(
                "에이전트 비동기 접수 식별자가 요청과 일치하지 않습니다.",
                error_type="agent_response_correlation_mismatch",
                retryable=False,
            )
        expected_status = (
            "accepted" if response.status_code == 202 else "duplicate"
        )
        if accepted.status != expected_status:
            raise AgentServiceError(
                "에이전트 비동기 접수 상태가 HTTP 상태와 일치하지 않습니다.",
                error_type="agent_response_status_invalid",
                retryable=False,
            )
        return accepted

    async def send_clinician_alert_async(self, payload: AgentAsyncClinicianAlertRequest) -> AgentInternalTaskAccepted:
        data = await self._request_json("POST", "/agent/async/clinician-alerts", payload=payload.model_dump(mode="json"), timeout=10.0)
        return self._validate_response_model(data, AgentInternalTaskAccepted, "에이전트 비동기 접수 응답을 해석하지 못했습니다.")


async def _iter_ndjson_objects(
    response: httpx.Response,
) -> AsyncIterator[dict[str, Any]]:
    async for line in response.aiter_lines():
        if len(line.encode("utf-8")) > MAX_STREAM_EVENT_BYTES:
            raise AgentServiceError(
                "에이전트 스트림 한 줄이 허용 크기를 초과했습니다.",
                error_type="agent_stream_invalid",
            )
        if not line:
            raise AgentServiceError(
                "에이전트 NDJSON 스트림에 빈 행이 포함되었습니다.",
                error_type="agent_stream_invalid",
            )
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AgentServiceError(
                "에이전트 NDJSON 행이 올바른 JSON이 아닙니다.",
                error_type="agent_stream_invalid",
            ) from exc
        if not isinstance(value, dict):
            raise AgentServiceError(
                "에이전트 NDJSON 이벤트는 JSON 객체여야 합니다.",
                error_type="agent_stream_invalid",
            )
        yield value


def _validate_stream_correlation(
    request: ChatSyncRequest,
    event: ChatStreamEvent,
) -> None:
    if (
        event.request_id != request.request_id
        or event.message_id != request.message_id
    ):
        raise AgentServiceError(
            "에이전트 스트림 식별자가 요청과 일치하지 않습니다.",
            error_type="agent_response_correlation_mismatch",
            retryable=False,
        )
