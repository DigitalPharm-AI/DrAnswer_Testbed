from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class AgentStateCipherError(RuntimeError):
    """Low-level authenticated-state token failure."""


class AgentStateCipher:
    """Shared AES-GCM token and keyed-digest mechanics for Agent DB state.

    Domain ciphers retain their own key validation, purpose namespaces,
    associated data, JSON shape checks, and public error types. Keeping those
    responsibilities in the wrappers preserves all existing ciphertext and
    digest formats while making the cryptographic primitive implementation a
    single source of truth.
    """

    _VERSION_PREFIX = "v1."
    _NONCE_BYTES = 12
    _MIN_TOKEN_BYTES = _NONCE_BYTES + 16 + 1

    def __init__(self, key: bytes) -> None:
        self._key = key
        self._cipher = AESGCM(key)

    def digest(self, value: bytes) -> str:
        return self.mac(value).hex()

    def mac(self, value: bytes) -> bytes:
        return hmac.new(self._key, value, hashlib.sha256).digest()

    def encrypt(self, plaintext: bytes, *, associated_data: bytes) -> str:
        nonce = os.urandom(self._NONCE_BYTES)
        ciphertext = self._cipher.encrypt(
            nonce,
            plaintext,
            associated_data,
        )
        token = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")
        return f"{self._VERSION_PREFIX}{token}"

    def decrypt(
        self,
        token: str,
        *,
        associated_data: bytes,
        version_error: str,
        authentication_error: str,
    ) -> bytes:
        if not token.startswith(self._VERSION_PREFIX):
            raise AgentStateCipherError(version_error)
        try:
            encoded = token.removeprefix(self._VERSION_PREFIX)
            payload = base64.urlsafe_b64decode(
                encoded + ("=" * (-len(encoded) % 4))
            )
            if len(payload) < self._MIN_TOKEN_BYTES:
                raise ValueError("ciphertext_too_short")
            return self._cipher.decrypt(
                payload[: self._NONCE_BYTES],
                payload[self._NONCE_BYTES :],
                associated_data,
            )
        except (InvalidTag, ValueError, binascii.Error) as exc:
            raise AgentStateCipherError(authentication_error) from exc
