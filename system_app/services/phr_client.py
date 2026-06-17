from __future__ import annotations

import httpx

from shared.schemas import PhrMedicationRegistrationItem, PhrPatientRegistrationRequest, PhrPatientRegistrationResult
from shared.settings import get_settings


class PhrServiceError(RuntimeError):
    pass


class PhrClient:
    def __init__(self, *, base_url: str | None = None, timeout_seconds: float = 30.0) -> None:
        settings = get_settings()
        self.base_url = (base_url or settings.phr_base_url).rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def _send_registration_request(self, method: str, path: str, request: PhrPatientRegistrationRequest) -> PhrPatientRegistrationResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.request(method, f"{self.base_url}{path}", json=request.model_dump(mode="json"))
                response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network dependent
            raise PhrServiceError(str(exc)) from exc
        return PhrPatientRegistrationResult.model_validate(response.json())

    async def register_patient(self, medications: list[PhrMedicationRegistrationItem]) -> PhrPatientRegistrationResult:
        request = PhrPatientRegistrationRequest(medications=medications)
        return await self._send_registration_request("POST", "/phr/patients/register", request)

    async def update_patient_medications(self, phr_patient_key: str, medications: list[PhrMedicationRegistrationItem]) -> PhrPatientRegistrationResult:
        request = PhrPatientRegistrationRequest(medications=medications)
        return await self._send_registration_request("PUT", f"/phr/patients/{phr_patient_key}/medications", request)
