from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_app.integration.feedback_contracts import ChatFeedbackRequest
from shared.settings import Settings


class FeedbackEncryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeedbackEncryptionContext:
    api_path: str
    request_id: str
    message_id: str
    conversation_id: str
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
        self._key = key
        self._cipher = AESGCM(key)

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
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            value.encode("utf-8"),
            context.associated_data(),
        )
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"v1.{token}"

    def decrypt(
        self,
        token: str,
        *,
        context: FeedbackEncryptionContext,
    ) -> str:
        if not token.startswith("v1."):
            raise FeedbackEncryptionError(
                "unsupported_feedback_ciphertext_version"
            )
        try:
            encoded = token.removeprefix("v1.")
            payload = base64.urlsafe_b64decode(
                encoded + ("=" * (-len(encoded) % 4))
            )
            if len(payload) < 29:
                raise ValueError("ciphertext_too_short")
            plaintext = self._cipher.decrypt(
                payload[:12],
                payload[12:],
                context.associated_data(),
            )
            return plaintext.decode("utf-8")
        except (
            InvalidTag,
            UnicodeDecodeError,
            ValueError,
            binascii.Error,
        ) as exc:
            raise FeedbackEncryptionError(
                "feedback_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return hmac.new(self._key, value, hashlib.sha256).hexdigest()
