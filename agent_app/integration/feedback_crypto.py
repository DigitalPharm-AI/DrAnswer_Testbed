from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from agent_app.integration.feedback_contracts import ChatFeedbackRequest
from agent_app.integration.state_crypto import (
    AgentStateCipher,
    AgentStateCipherError,
)
from shared.settings import Settings


class FeedbackEncryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackEncryptionContext:
    api_path: str
    request_id: str
    message_id: str
    patient_id_hash: str
    feedback_at: str

    def associated_data(self) -> bytes:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class FeedbackCipher:
    """AES-256-GCM storage encryption with keyed, non-reversible digests."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("feedback_encryption_key_must_be_32_bytes")
        if not key_id.strip():
            raise ValueError("feedback_encryption_key_id_required")
        self.key_id = key_id.strip()
        self._state_cipher = AgentStateCipher(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> FeedbackCipher:
        key_id, key = settings.require_agent_feedback_encryption()
        return cls(key_id=key_id, key=key)

    def request_digest(self, payload: ChatFeedbackRequest) -> str:
        canonical = json.dumps(
            payload.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return self._digest(b"feedback-request-v1\0" + canonical)

    def text_digest(self, value: str) -> str:
        return self._digest(
            b"feedback-text-v1\0" + value.encode("utf-8")
        )

    def patient_digest(self, patient_id: str) -> str:
        return self._digest(
            b"patient-id-v1\0" + patient_id.encode("utf-8")
        )

    def encrypt(
        self,
        value: str,
        *,
        context: FeedbackEncryptionContext,
    ) -> str:
        return self._state_cipher.encrypt(
            value.encode("utf-8"),
            associated_data=context.associated_data(),
        )

    def decrypt(
        self,
        token: str,
        *,
        context: FeedbackEncryptionContext,
    ) -> str:
        try:
            plaintext = self._state_cipher.decrypt(
                token,
                associated_data=context.associated_data(),
                version_error="unsupported_feedback_ciphertext_version",
                authentication_error=(
                    "feedback_ciphertext_authentication_failed"
                ),
            )
            return plaintext.decode("utf-8")
        except AgentStateCipherError as exc:
            raise FeedbackEncryptionError(str(exc)) from exc
        except UnicodeDecodeError as exc:
            raise FeedbackEncryptionError(
                "feedback_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return self._state_cipher.digest(value)
