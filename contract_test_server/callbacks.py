from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from contract_test_server.config import ContractServerSettings
from contract_test_server.storage import CALLBACK_PATHS, ClaimedCallback, ContractStore
from shared.async_v13_contracts import (
    AsyncResultCallbackAck,
    NotificationPolicyProposalAck,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    callback_request_id: str | None = None


class CallbackDispatcher:
    def __init__(
        self,
        *,
        settings: ContractServerSettings,
        store: ContractStore,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.transport = transport

    def deliver_one(self) -> DeliveryResult:
        if (
            self.settings.callback_mode != "deliver"
            or not self.settings.callback_delivery_configured
        ):
            return DeliveryResult(delivered=False)
        job = self.store.claim_due_callback(
            lease_seconds=max(
                30,
                self.settings.callback_timeout_seconds * 2,
            ),
            max_attempts=self.settings.callback_max_attempts,
        )
        if job is None:
            return DeliveryResult(delivered=False)
        self._deliver_claimed(job)
        return DeliveryResult(
            delivered=True,
            callback_request_id=job.callback_request_id,
        )

    def _deliver_claimed(self, job: ClaimedCallback) -> None:
        if job.callback_path not in CALLBACK_PATHS:
            updated = self.store.mark_callback_dead(
                job.callback_request_id,
                claim_id=job.claim_id,
                http_status=None,
                error_code="callback_path_not_allowlisted",
            )
            if not updated:
                self._log_stale_claim(job)
            return

        url = (
            self.settings.backend_callback_base_url.rstrip("/")
            + job.callback_path
        )
        headers = {
            "Authorization": (
                f"Bearer {self.settings.agent_sync_api_token}"
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(
                timeout=self.settings.callback_timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = client.post(
                    url,
                    headers=headers,
                    content=job.payload_bytes,
                )
        except httpx.HTTPError:
            self._retry_or_dead(
                job,
                http_status=None,
                error_code="callback_transport_error",
            )
            return

        if self._valid_ack(job, response):
            updated = self.store.mark_callback_completed(
                job.callback_request_id,
                claim_id=job.claim_id,
                http_status=response.status_code,
            )
            if not updated:
                self._log_stale_claim(job)
                return
            logger.info(
                "callback_delivery path=%s request_id=%s status=completed attempt=%s",
                job.callback_path,
                job.callback_request_id,
                job.attempt,
            )
            return

        if response.status_code in {408, 429} or response.status_code >= 500:
            error_code = f"callback_transient_http_{response.status_code}"
            self._retry_or_dead(
                job,
                http_status=response.status_code,
                error_code=error_code,
            )
            return

        if 200 <= response.status_code < 300:
            self._retry_or_dead(
                job,
                http_status=response.status_code,
                error_code="callback_invalid_ack",
            )
            return

        updated = self.store.mark_callback_dead(
            job.callback_request_id,
            claim_id=job.claim_id,
            http_status=response.status_code,
            error_code=f"callback_permanent_http_{response.status_code}",
        )
        if not updated:
            self._log_stale_claim(job)
            return
        logger.warning(
            "callback_delivery path=%s request_id=%s status=dead attempt=%s http_status=%s",
            job.callback_path,
            job.callback_request_id,
            job.attempt,
            response.status_code,
        )

    def _valid_ack(
        self,
        job: ClaimedCallback,
        response: httpx.Response,
    ) -> bool:
        try:
            payload: Any = json.loads(response.content)
            if job.callback_kind == "missed_dose_result":
                if response.status_code != 200:
                    return False
                ack = AsyncResultCallbackAck.model_validate(payload)
                return ack.request_id == job.callback_request_id

            if job.callback_kind == "notification_policy_proposal":
                ack = NotificationPolicyProposalAck.model_validate(payload)
                if ack.request_id != job.callback_request_id:
                    return False
                return (
                    response.status_code == 202
                    and ack.status == "accepted"
                ) or (
                    response.status_code == 200
                    and ack.status == "duplicate"
                )
            return False
        except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
            return False

    def _retry_or_dead(
        self,
        job: ClaimedCallback,
        *,
        http_status: int | None,
        error_code: str,
    ) -> None:
        if job.attempt >= self.settings.callback_max_attempts:
            updated = self.store.mark_callback_dead(
                job.callback_request_id,
                claim_id=job.claim_id,
                http_status=http_status,
                error_code=f"retry_exhausted:{error_code}",
            )
            status = "dead"
        else:
            delay = self.settings.callback_retry_base_seconds * (
                2 ** (job.attempt - 1)
            )
            updated = self.store.mark_callback_retry(
                job.callback_request_id,
                claim_id=job.claim_id,
                http_status=http_status,
                error_code=error_code,
                delay_seconds=delay,
            )
            status = "retry_wait"
        if not updated:
            self._log_stale_claim(job)
            return
        logger.warning(
            "callback_delivery path=%s request_id=%s status=%s attempt=%s http_status=%s",
            job.callback_path,
            job.callback_request_id,
            status,
            job.attempt,
            http_status,
        )

    @staticmethod
    def _log_stale_claim(job: ClaimedCallback) -> None:
        logger.warning(
            "callback_delivery path=%s request_id=%s status=stale_claim attempt=%s",
            job.callback_path,
            job.callback_request_id,
            job.attempt,
        )
