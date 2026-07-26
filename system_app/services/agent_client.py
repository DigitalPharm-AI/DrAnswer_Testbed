from __future__ import annotations

import asyncio
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from shared.chat_contracts import ChatSyncRequest, ChatSyncResponse
from shared.schemas import (
    AgentAsyncAccepted,
    AgentAsyncClinicianAlertRequest,
    AgentModelConfig,
    AgentModelTierRequest,
    AgentResponse,
    DailyMedicationPattern,
    MissedDoseEventPayload,
    MultiturnChatRequest,
    MutationConfirmationResolutionRequest,
)
from shared.settings import get_settings


class AgentServiceError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str = "agent_service_error",
        trace_id: str | None = None,
        agent_name: str | None = None,
        decision_type: str | None = None,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.trace_id = trace_id
        self.agent_name = agent_name
        self.decision_type = decision_type
        self.retryable = retryable
        self.details = details


class AgentClient:
    def __init__(self, base_url: str | None = None) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.agent_base_url).rstrip("/")
        self.internal_api_token = settings.internal_api_token
        self.agent_sync_api_token = settings.require_agent_sync_api_token()
        self.llm_timeout_seconds = settings.llm_timeout_seconds

    def _headers(self) -> dict[str, str]:
        if not self.internal_api_token:
            return {}
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
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                response = await client.request(method, f"{self.base_url}{path}", json=payload, headers=self._headers())
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

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> AgentResponse:
        data = await self._request_json("POST", path, payload=payload, timeout=timeout)
        return self._validate_response_model(data, AgentResponse, "에이전트 성공 응답을 해석하지 못했습니다.")

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

        error = payload.get("error") if isinstance(payload, dict) and isinstance(payload.get("error"), dict) else {}
        message = error.get("message") or (payload.get("message") if isinstance(payload, dict) else None)
        if not message:
            message = f"에이전트 서버가 {response.status_code} 오류를 반환했습니다."

        return AgentServiceError(
            message,
            status_code=response.status_code,
            error_type=error.get("code") or (payload.get("error_type", "agent_service_error") if isinstance(payload, dict) else "agent_service_error"),
            trace_id=payload.get("trace_id") if isinstance(payload, dict) else None,
            agent_name=payload.get("agent_name") if isinstance(payload, dict) else None,
            decision_type=payload.get("decision_type") if isinstance(payload, dict) else None,
            retryable=bool(error.get("retryable")),
            details=error.get("details") if isinstance(error.get("details"), dict) else None,
        )

    async def send_multiturn_chat(self, payload: MultiturnChatRequest) -> AgentResponse:
        return await self._post("/agent/multiturn-chat", payload.model_dump(mode="json"))

    async def send_sync_chat(self, payload: ChatSyncRequest) -> ChatSyncResponse:
        settings = get_settings()
        body = payload.model_dump(mode="json")
        max_retries = max(0, settings.agent_sync_max_retries)
        timeout = max(
            1.0,
            float(settings.agent_sync_chat_timeout_seconds) + 5.0,
        )
        for attempt in range(max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    response = await client.post(
                        f"{self.base_url}/agent/sync/chat",
                        json=body,
                        headers=self._sync_headers(),
                    )
                if response.status_code in {503, 504} and attempt < max_retries:
                    await asyncio.sleep(float(2**attempt))
                    continue
                response.raise_for_status()
                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgentServiceError(
                        "에이전트 응답을 JSON으로 해석하지 못했습니다.",
                        status_code=response.status_code,
                        error_type="agent_response_invalid",
                    ) from exc
                parsed = self._validate_response_model(
                    data,
                    ChatSyncResponse,
                    "에이전트 v1.2 채팅 응답을 해석하지 못했습니다.",
                )
                self._validate_sync_chat_correlation(payload, parsed)
                return parsed
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
        raise RuntimeError("unreachable_sync_chat_retry_state")

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

    async def resolve_mutation_confirmation(
        self,
        payload: MutationConfirmationResolutionRequest,
    ) -> AgentResponse:
        return await self._post(
            "/agent/mutation-confirmations/resolve",
            payload.model_dump(mode="json"),
            timeout=max(90.0, float(self.llm_timeout_seconds) + 30.0),
        )

    async def send_daily_pattern_async(self, payload: DailyMedicationPattern) -> AgentAsyncAccepted:
        data = await self._request_json("POST", "/agent/async/daily-patterns", payload=payload.model_dump(mode="json"), timeout=10.0)
        return self._validate_response_model(data, AgentAsyncAccepted, "에이전트 비동기 접수 응답을 해석하지 못했습니다.")

    async def send_missed_dose_async(self, payload: MissedDoseEventPayload) -> AgentAsyncAccepted:
        data = await self._request_json("POST", "/agent/async/missed-dose-events", payload=payload.model_dump(mode="json"), timeout=10.0)
        return self._validate_response_model(data, AgentAsyncAccepted, "에이전트 비동기 접수 응답을 해석하지 못했습니다.")

    async def send_chat_continuation_async(self, payload: MultiturnChatRequest) -> AgentAsyncAccepted:
        data = await self._request_json("POST", "/agent/async/chat-continuations", payload=payload.model_dump(mode="json"), timeout=10.0)
        return self._validate_response_model(data, AgentAsyncAccepted, "에이전트 비동기 접수 응답을 해석하지 못했습니다.")

    async def send_clinician_alert_async(self, payload: AgentAsyncClinicianAlertRequest) -> AgentAsyncAccepted:
        data = await self._request_json("POST", "/agent/async/clinician-alerts", payload=payload.model_dump(mode="json"), timeout=10.0)
        return self._validate_response_model(data, AgentAsyncAccepted, "에이전트 비동기 접수 응답을 해석하지 못했습니다.")
