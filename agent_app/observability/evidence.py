from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from shared.settings import Settings


class TraceEvidenceEncryptionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TraceEvidenceContext:
    trace_id: str
    observation_id: str
    step_type: str
    sequence: int
    trace_attempt_number: int

    def associated_data(self) -> bytes:
        return json.dumps(
            asdict(self),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


class TraceEvidenceCipher:
    """Encrypt auditable decision evidence kept only in the Agent Trace DB."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(
                "trace_evidence_encryption_key_must_be_32_bytes"
            )
        if not key_id.strip():
            raise ValueError(
                "trace_evidence_encryption_key_id_required"
            )
        self.key_id = key_id.strip()
        self._key = key
        self._cipher = AESGCM(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> TraceEvidenceCipher:
        root_key_id, root_key = (
            settings.require_agent_feedback_encryption()
        )
        derived_key = hmac.new(
            root_key,
            b"agent-trace-decision-evidence-v1",
            hashlib.sha256,
        ).digest()
        return cls(
            key_id=f"{root_key_id}:trace-v1",
            key=derived_key,
        )

    def encrypt_json(
        self,
        value: Any,
        *,
        context: TraceEvidenceContext,
    ) -> tuple[str, str]:
        canonical = _canonical_json(value)
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            canonical,
            context.associated_data(),
        )
        token = base64.urlsafe_b64encode(
            nonce + ciphertext
        ).decode("ascii")
        digest = hmac.new(
            self._key,
            b"trace-evidence-v1\0" + canonical,
            hashlib.sha256,
        ).hexdigest()
        return f"v1.{token}", digest

    def decrypt_json(
        self,
        token: str,
        *,
        context: TraceEvidenceContext,
    ) -> Any:
        if not token.startswith("v1."):
            raise TraceEvidenceEncryptionError(
                "unsupported_trace_evidence_ciphertext_version"
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
            return json.loads(plaintext.decode("utf-8"))
        except (
            InvalidTag,
            UnicodeDecodeError,
            ValueError,
            json.JSONDecodeError,
            binascii.Error,
        ) as exc:
            raise TraceEvidenceEncryptionError(
                "trace_evidence_ciphertext_authentication_failed"
            ) from exc


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
