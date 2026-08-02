from __future__ import annotations

import base64
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.state_crypto import (
    AgentStateCipher,
    AgentStateCipherError,
)
from agent_app.integration.write_state import canonical_payload_hash
from agent_app.persistence.models import AgentPendingAction
from shared.json_utils import canonical_json
from shared.settings import Settings, get_settings
from shared.time_utils import utc_now

PENDING = "PENDING"
APPROVED = "APPROVED"
SENDING = "SENDING"
CONSUMED = "CONSUMED"
CANCELLED = "CANCELLED"
EXPIRED = "EXPIRED"
SUPERSEDED = "SUPERSEDED"
APPROVAL_TTL_SECONDS = 30 * 60
SENDING_LEASE_SECONDS = 2 * 60


class InternalApprovalError(RuntimeError):
    """The requested write is not bound to a valid AI-owned approval."""


class InternalApprovalEncryptionError(InternalApprovalError):
    pass


@dataclass(frozen=True)
class ApprovalEncryptionContext:
    approval_id: str
    patient_id_hash: str
    action_name: str
    action_fingerprint: str

    def associated_data(self) -> bytes:
        return canonical_json(asdict(self)).encode("utf-8")


class InternalApprovalCipher:
    """Encrypt pending write arguments and derive one-time capability keys."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("internal_approval_encryption_key_must_be_32_bytes")
        if not key_id.strip():
            raise ValueError("internal_approval_encryption_key_id_required")
        self.key_id = key_id.strip()
        self._state_cipher = AgentStateCipher(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> InternalApprovalCipher:
        key_id, key = settings.require_agent_feedback_encryption()
        return cls(key_id=key_id, key=key)

    def patient_digest(self, patient_id: str) -> str:
        return self._digest(
            b"agent-approval-patient-v1\0" + patient_id.encode("utf-8")
        )

    def payload_digest(self, payload: dict[str, Any]) -> str:
        return self._digest(
            b"agent-approval-payload-v1\0"
            + canonical_json(payload).encode("utf-8")
        )

    def capability_key(
        self,
        *,
        approval_id: str,
        confirmation_message_id: str,
    ) -> str:
        digest = self._state_cipher.mac(
            b"agent-approval-capability-v1\0"
            + approval_id.encode("utf-8")
            + b"\0"
            + confirmation_message_id.encode("utf-8")
        )[:18]
        token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return f"apv_{token}"

    def capability_digest(self, approval_key: str) -> str:
        return self._digest(
            b"agent-approval-capability-hash-v1\0"
            + approval_key.encode("utf-8")
        )

    def encrypt_payload(
        self,
        payload: dict[str, Any],
        *,
        context: ApprovalEncryptionContext,
    ) -> str:
        return self._state_cipher.encrypt(
            canonical_json(payload).encode("utf-8"),
            associated_data=context.associated_data(),
        )

    def decrypt_payload(
        self,
        token: str,
        *,
        context: ApprovalEncryptionContext,
        expected_hash: str,
    ) -> dict[str, Any]:
        try:
            value = json.loads(
                self._state_cipher.decrypt(
                    token,
                    associated_data=context.associated_data(),
                    version_error=(
                        "internal_approval_ciphertext_version_invalid"
                    ),
                    authentication_error=(
                        "internal_approval_ciphertext_authentication_failed"
                    ),
                ).decode("utf-8")
            )
            if not isinstance(value, dict):
                raise ValueError("approval_payload_not_object")
            if not hmac.compare_digest(
                self.payload_digest(value),
                expected_hash,
            ):
                raise ValueError("approval_payload_hash_mismatch")
            return value
        except AgentStateCipherError as exc:
            raise InternalApprovalEncryptionError(str(exc)) from exc
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise InternalApprovalEncryptionError(
                "internal_approval_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return self._state_cipher.digest(value)


@dataclass(frozen=True)
class InternalApprovalProposal:
    action_name: str
    action_fingerprint: str
    display: dict[str, Any]

    def public_payload(self) -> dict[str, Any]:
        return {
            "confirmation_required": True,
            "action_type": "agent_tool",
            "action_name": self.action_name,
            "status": "pending",
            "display": self.display,
        }


@dataclass(frozen=True)
class InternalApprovalDecision:
    kind: Literal["approved", "cancelled"]
    action_name: str
    approval_key: str = ""


@dataclass(frozen=True)
class InternalApprovalGrant:
    approval_key: str
    action_name: str
    action_fingerprint: str
    arguments: dict[str, Any]
    confirmation_message_id: str
    source_chat_request_id: str
    expected_version: int | None


@dataclass(frozen=True)
class InternalApprovalBinding:
    approval_key: str
    action_name: str
    action_fingerprint: str
    patient_id: str
    source_chat_request_id: str
    confirmation_message_id: str
    write_request_id: str
    argument_hash: str
    request_body_hash: str
    expected_version: int | None


class InternalApprovalStore:
    """Restart-safe, Agent-DB-owned approval and one-time capability store."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings | None = None,
        cipher: InternalApprovalCipher | None = None,
        ttl_seconds: int = APPROVAL_TTL_SECONDS,
        sending_lease_seconds: int = SENDING_LEASE_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher or InternalApprovalCipher.from_settings(
            settings or get_settings()
        )
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.sending_lease_seconds = max(1, int(sending_lease_seconds))

    def prepare(
        self,
        *,
        patient_id: str,
        source_chat_request_id: str,
        source_message_id: str,
        trace_id: str,
        action_name: str,
        tool_call_id: str,
        arguments: dict[str, Any],
        display: dict[str, Any],
    ) -> InternalApprovalProposal:
        required = {
            "patient_id": patient_id,
            "source_chat_request_id": source_chat_request_id,
            "source_message_id": source_message_id,
            "action_name": action_name,
        }
        missing = [
            name for name, value in required.items() if not str(value).strip()
        ]
        if missing:
            raise InternalApprovalError(
                "internal_approval_context_missing:"
                + ",".join(sorted(missing))
            )
        now = utc_now()
        patient_hash = self.cipher.patient_digest(patient_id)
        action_fingerprint = canonical_payload_hash(
            {"action_name": action_name, "arguments": arguments}
        )
        with self.session_factory() as session:
            existing = session.scalar(
                select(AgentPendingAction)
                .where(
                    AgentPendingAction.patient_id_hash == patient_hash,
                    AgentPendingAction.source_chat_request_id
                    == source_chat_request_id,
                    AgentPendingAction.source_message_id == source_message_id,
                    AgentPendingAction.action_name == action_name,
                    AgentPendingAction.action_fingerprint
                    == action_fingerprint,
                    AgentPendingAction.status == PENDING,
                )
                .with_for_update()
            )
            if existing is not None:
                payload = self._decrypt_row(existing)
                stored_display = payload.get("display")
                if not isinstance(stored_display, dict):
                    raise InternalApprovalError(
                        "internal_approval_display_missing"
                    )
                return InternalApprovalProposal(
                    action_name=existing.action_name,
                    action_fingerprint=existing.action_fingerprint,
                    display=stored_display,
                )

            session.execute(
                update(AgentPendingAction)
                .where(
                    AgentPendingAction.patient_id_hash == patient_hash,
                    AgentPendingAction.status.in_(
                        (PENDING, APPROVED, SENDING)
                    ),
                )
                .values(
                    status=SUPERSEDED,
                    resolved_at=now,
                    updated_at=now,
                )
            )
            approval_id = f"approval_{uuid4().hex}"
            approval_payload = {
                "arguments": arguments,
                "display": display,
            }
            encryption_context = ApprovalEncryptionContext(
                approval_id=approval_id,
                patient_id_hash=patient_hash,
                action_name=action_name,
                action_fingerprint=action_fingerprint,
            )
            session.add(
                AgentPendingAction(
                    public_id=approval_id,
                    source_chat_request_id=source_chat_request_id,
                    source_message_id=source_message_id,
                    trace_id=trace_id,
                    patient_id_hash=patient_hash,
                    action_type="agent_tool",
                    action_name=action_name,
                    tool_call_id=tool_call_id,
                    action_fingerprint=action_fingerprint,
                    display_json="{}",
                    payload_ciphertext=self.cipher.encrypt_payload(
                        approval_payload,
                        context=encryption_context,
                    ),
                    payload_hash=self.cipher.payload_digest(
                        approval_payload
                    ),
                    encryption_key_id=self.cipher.key_id,
                    approval_key_hash="",
                    status=PENDING,
                    created_at=now,
                    updated_at=now,
                    action_expires_at=now
                    + timedelta(seconds=self.ttl_seconds),
                    expires_at=now + timedelta(days=3 * 365),
                )
            )
            session.commit()
        return InternalApprovalProposal(
            action_name=action_name,
            action_fingerprint=action_fingerprint,
            display=display,
        )

    def submit_response(
        self,
        *,
        patient_id: str,
        current_user_message_id: str,
        current_source_chat_request_id: str,
        originating_user_message_id: str,
        submitted_value: str,
        source_message: dict[str, Any],
    ) -> InternalApprovalDecision | None:
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingAction)
                .where(
                    AgentPendingAction.patient_id_hash == patient_hash,
                    AgentPendingAction.source_message_id
                    == originating_user_message_id,
                    AgentPendingAction.status.in_(
                        (
                            PENDING,
                            APPROVED,
                            SENDING,
                            CONSUMED,
                            CANCELLED,
                            EXPIRED,
                            SUPERSEDED,
                        )
                    ),
                )
                .order_by(AgentPendingAction.id.desc())
                .with_for_update()
            )
            if row is None:
                if _looks_like_approval_card(source_message):
                    raise InternalApprovalError(
                        "internal_approval_not_found"
                    )
                return None
            payload = self._decrypt_row(row)
            display = payload.get("display")
            if not isinstance(display, dict):
                raise InternalApprovalError(
                    "internal_approval_display_missing"
                )
            if not _source_card_matches(display, source_message):
                raise InternalApprovalError(
                    "internal_approval_source_card_mismatch"
                )
            action_label = str(
                display.get("action_label") or "변경 적용"
            ).strip()
            selected = submitted_value.strip()
            if selected not in {action_label, "취소"}:
                return None

            if row.action_expires_at <= now and row.status not in {
                CONSUMED,
                CANCELLED,
            }:
                row.status = EXPIRED
                row.resolved_at = now
                row.updated_at = now
                session.commit()
                raise InternalApprovalError("internal_approval_expired")

            if selected == "취소":
                if row.status in {SUPERSEDED, EXPIRED}:
                    raise InternalApprovalError(
                        f"internal_approval_not_executable:{row.status}"
                    )
                if row.status in {SENDING, CONSUMED}:
                    raise InternalApprovalError(
                        "internal_approval_already_executed"
                    )
                if row.status == APPROVED and (
                    row.confirmation_message_id != current_user_message_id
                    or row.approved_source_chat_request_id
                    != current_source_chat_request_id
                ):
                    raise InternalApprovalError(
                        "internal_approval_response_conflict"
                    )
                row.status = CANCELLED
                row.approval_key_hash = ""
                row.confirmation_message_id = current_user_message_id
                row.approved_source_chat_request_id = (
                    current_source_chat_request_id
                )
                row.resolved_at = now
                row.updated_at = now
                session.commit()
                return InternalApprovalDecision(
                    kind="cancelled",
                    action_name=row.action_name,
                )

            if row.status == CANCELLED:
                raise InternalApprovalError(
                    "internal_approval_already_cancelled"
                )
            if row.status in {SUPERSEDED, EXPIRED}:
                raise InternalApprovalError(
                    f"internal_approval_not_executable:{row.status}"
                )
            if row.status in {APPROVED, SENDING, CONSUMED}:
                if (
                    row.confirmation_message_id != current_user_message_id
                    or row.approved_source_chat_request_id
                    != current_source_chat_request_id
                ):
                    raise InternalApprovalError(
                        "internal_approval_response_conflict"
                    )
            else:
                row.status = APPROVED
                row.confirmation_message_id = current_user_message_id
                row.approved_source_chat_request_id = (
                    current_source_chat_request_id
                )
                row.updated_at = now
            approval_key = self.cipher.capability_key(
                approval_id=row.public_id,
                confirmation_message_id=current_user_message_id,
            )
            key_hash = self.cipher.capability_digest(approval_key)
            if row.approval_key_hash and not hmac.compare_digest(
                row.approval_key_hash,
                key_hash,
            ):
                raise InternalApprovalError(
                    "internal_approval_key_binding_conflict"
                )
            row.approval_key_hash = key_hash
            session.commit()
            return InternalApprovalDecision(
                kind="approved",
                action_name=row.action_name,
                approval_key=approval_key,
            )

    def resolve_grant(
        self,
        *,
        approval_key: str,
        patient_id: str,
        action_name: str,
        source_chat_request_id: str,
        confirmation_message_id: str,
    ) -> InternalApprovalGrant:
        if not approval_key.startswith("apv_"):
            raise InternalApprovalError("internal_approval_key_invalid")
        key_hash = self.cipher.capability_digest(approval_key)
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingAction).where(
                    AgentPendingAction.approval_key_hash == key_hash
                )
            )
            if row is None:
                raise InternalApprovalError("internal_approval_not_found")
            if row.patient_id_hash != patient_hash:
                raise InternalApprovalError(
                    "internal_approval_patient_mismatch"
                )
            if row.action_name != action_name:
                raise InternalApprovalError(
                    "internal_approval_action_mismatch"
                )
            if (
                row.approved_source_chat_request_id
                != source_chat_request_id
                or row.confirmation_message_id
                != confirmation_message_id
            ):
                raise InternalApprovalError(
                    "internal_approval_message_binding_mismatch"
                )
            if row.status not in {APPROVED, SENDING, CONSUMED}:
                raise InternalApprovalError(
                    f"internal_approval_not_executable:{row.status}"
                )
            if row.action_expires_at <= now and row.status != CONSUMED:
                raise InternalApprovalError("internal_approval_expired")
            payload = self._decrypt_row(row)
            arguments = payload.get("arguments")
            if not isinstance(arguments, dict):
                raise InternalApprovalError(
                    "internal_approval_arguments_missing"
                )
            return InternalApprovalGrant(
                approval_key=approval_key,
                action_name=row.action_name,
                action_fingerprint=row.action_fingerprint,
                arguments=arguments,
                confirmation_message_id=row.confirmation_message_id,
                source_chat_request_id=(
                    row.approved_source_chat_request_id
                ),
                expected_version=row.expected_version,
            )

    def claim(self, binding: InternalApprovalBinding) -> None:
        now = utc_now()
        key_hash = self.cipher.capability_digest(binding.approval_key)
        patient_hash = self.cipher.patient_digest(binding.patient_id)
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingAction)
                .where(AgentPendingAction.approval_key_hash == key_hash)
                .with_for_update()
            )
            if row is None:
                raise InternalApprovalError("internal_approval_not_found")
            self._validate_row(row, binding, patient_hash=patient_hash)
            if row.status == CONSUMED:
                raise InternalApprovalError(
                    "internal_approval_already_consumed"
                )
            if row.status == SENDING:
                lease_started_at = row.execution_started_at
                lease_expires_at = (
                    lease_started_at
                    + timedelta(seconds=self.sending_lease_seconds)
                    if lease_started_at is not None
                    else None
                )
                if lease_expires_at is not None and lease_expires_at > now:
                    raise InternalApprovalError(
                        "internal_approval_already_sending"
                    )
                row.send_attempt_count += 1
                row.execution_started_at = now
                row.updated_at = now
                session.commit()
                return
            if row.status != APPROVED:
                raise InternalApprovalError(
                    f"internal_approval_not_executable:{row.status}"
                )
            if row.action_expires_at <= now:
                row.status = EXPIRED
                row.resolved_at = now
                row.updated_at = now
                session.commit()
                raise InternalApprovalError("internal_approval_expired")
            row.write_request_id = binding.write_request_id
            row.argument_hash = binding.argument_hash
            row.request_body_hash = binding.request_body_hash
            row.expected_version = binding.expected_version
            row.status = SENDING
            row.send_attempt_count += 1
            row.execution_started_at = now
            row.updated_at = now
            session.commit()

    def validate_consumed_replay(
        self,
        binding: InternalApprovalBinding,
    ) -> None:
        key_hash = self.cipher.capability_digest(binding.approval_key)
        patient_hash = self.cipher.patient_digest(binding.patient_id)
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingAction).where(
                    AgentPendingAction.approval_key_hash == key_hash
                )
            )
            if row is None or row.status != CONSUMED:
                raise InternalApprovalError(
                    "internal_approval_replay_not_consumed"
                )
            self._validate_row(row, binding, patient_hash=patient_hash)

    def release_retry(self, approval_key: str) -> None:
        key_hash = self.cipher.capability_digest(approval_key)
        now = utc_now()
        with self.session_factory() as session:
            session.execute(
                update(AgentPendingAction)
                .where(
                    AgentPendingAction.approval_key_hash == key_hash,
                    AgentPendingAction.status == SENDING,
                )
                .values(
                    status=APPROVED,
                    execution_started_at=None,
                    updated_at=now,
                )
            )
            session.commit()

    def consume(self, approval_key: str, *, result: object) -> None:
        key_hash = self.cipher.capability_digest(approval_key)
        now = utc_now()
        with self.session_factory() as session:
            changed = session.execute(
                update(AgentPendingAction)
                .where(
                    AgentPendingAction.approval_key_hash == key_hash,
                    AgentPendingAction.status == SENDING,
                )
                .values(
                    status=CONSUMED,
                    result_hash=canonical_payload_hash(result),
                    resolved_at=now,
                    updated_at=now,
                )
            ).rowcount
            session.commit()
            if changed != 1:
                raise InternalApprovalError(
                    "internal_approval_consume_state_invalid"
                )

    def _decrypt_row(self, row: AgentPendingAction) -> dict[str, Any]:
        if row.encryption_key_id != self.cipher.key_id:
            raise InternalApprovalEncryptionError(
                "internal_approval_encryption_key_unavailable"
            )
        return self.cipher.decrypt_payload(
            row.payload_ciphertext,
            context=ApprovalEncryptionContext(
                approval_id=row.public_id,
                patient_id_hash=row.patient_id_hash,
                action_name=row.action_name,
                action_fingerprint=row.action_fingerprint,
            ),
            expected_hash=row.payload_hash,
        )

    @staticmethod
    def _validate_row(
        row: AgentPendingAction,
        binding: InternalApprovalBinding,
        *,
        patient_hash: str,
    ) -> None:
        if (
            row.patient_id_hash != patient_hash
            or row.action_name != binding.action_name
            or row.action_fingerprint != binding.action_fingerprint
            or row.approved_source_chat_request_id
            != binding.source_chat_request_id
            or row.confirmation_message_id
            != binding.confirmation_message_id
        ):
            raise InternalApprovalError(
                "internal_approval_binding_mismatch"
            )
        if row.write_request_id:
            persisted = (
                row.write_request_id,
                row.argument_hash,
                row.request_body_hash,
                row.expected_version,
            )
            actual = (
                binding.write_request_id,
                binding.argument_hash,
                binding.request_body_hash,
                binding.expected_version,
            )
            if persisted != actual:
                raise InternalApprovalError(
                    "internal_approval_request_mutated"
                )


def _source_card_matches(
    display: dict[str, Any],
    source_message: dict[str, Any],
) -> bool:
    card = source_message.get("message")
    if not isinstance(card, dict):
        return False
    expected_title = str(display.get("title") or "").strip()
    actual_title = str(card.get("message_title") or "").strip()
    if expected_title and actual_title != expected_title:
        return False
    action_label = str(
        display.get("action_label") or "변경 적용"
    ).strip()
    selections = {
        str(value).strip()
        for value in (card.get("selections") or [])
        if str(value).strip()
    }
    return action_label in selections and "취소" in selections


def _looks_like_approval_card(source_message: dict[str, Any]) -> bool:
    card = source_message.get("message")
    if not isinstance(card, dict):
        return False
    selections = {
        str(value).strip()
        for value in (card.get("selections") or [])
        if str(value).strip()
    }
    return "취소" in selections and bool(
        selections.intersection(
            {"기록", "수정", "삭제", "변경", "유지", "적용", "변경 적용"}
        )
    )
