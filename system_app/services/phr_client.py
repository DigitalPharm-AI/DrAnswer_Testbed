from __future__ import annotations

import httpx

from shared.schemas import PhrMedicationRegistrationItem, PhrPatientRegistrationRequest, PhrPatientRegistrationResult
from shared.settings import get_settings
from system_app.services.failure_copy import copy_for_phr_error

PUBLIC_PHR_ERROR_DETAILS = {
    "phr_network_error",
    "phr_patient_not_found",
    "phr_read_only_mode",
    "phr_response_invalid",
    "phr_upstream_error",
}


class PhrServiceError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, detail: str = "phr_network_error") -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


class PhrClient:
    def __init__(self, *, base_url: str | None = None, timeout_seconds: float = 30.0) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.phr_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def _send_registration_request(self, method: str, path: str, request: PhrPatientRegistrationRequest) -> PhrPatientRegistrationResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, trust_env=False) as client:
                response = await client.request(method, f"{self.base_url}{path}", json=request.model_dump(mode="json"))
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:  # pragma: no cover - network dependent
            detail = _response_detail(exc.response)
            copy = copy_for_phr_error(detail, status_code=exc.response.status_code)
            raise PhrServiceError(copy.body, status_code=exc.response.status_code, detail=detail) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - network dependent
            copy = copy_for_phr_error("phr_network_error")
            raise PhrServiceError(copy.body, detail="phr_network_error") from exc
        try:
            return PhrPatientRegistrationResult.model_validate(response.json())
        except Exception as exc:  # pragma: no cover - invalid upstream contract
            raise PhrServiceError("PHR 서버 응답을 해석하지 못했습니다. 잠시 후 다시 등록해주세요.", detail="phr_response_invalid") from exc

    async def register_patient(self, medications: list[PhrMedicationRegistrationItem]) -> PhrPatientRegistrationResult:
        request = PhrPatientRegistrationRequest(medications=medications)
        return await self._send_registration_request("POST", "/phr/patients/register", request)

    async def update_patient_medications(self, phr_patient_key: str, medications: list[PhrMedicationRegistrationItem]) -> PhrPatientRegistrationResult:
        request = PhrPatientRegistrationRequest(medications=medications)
        return await self._send_registration_request("PUT", f"/phr/patients/{phr_patient_key}/medications", request)


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return ""
    if isinstance(payload, dict) and isinstance(payload.get("detail"), str):
        detail = payload["detail"].strip()
        return detail if detail in PUBLIC_PHR_ERROR_DETAILS else "phr_upstream_error"
    return ""
