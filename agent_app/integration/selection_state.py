from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.persistence.models import AgentPendingSelection
from shared.backend_v13_contracts import (
    NutritionMealMutationPayload,
)
from shared.json_utils import canonical_json
from shared.schemas import AgentResponse
from shared.settings import Settings
from shared.time_utils import utc_now

PENDING = "PENDING"
RESOLVED = "RESOLVED"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
SUPERSEDED = "SUPERSEDED"
ACTIVE_STATUSES = (PENDING, RESOLVED)
NUTRITION_FOOD = "nutrition_food"
MEAL_TYPES = frozenset(
    {"breakfast", "lunch", "dinner", "snack"}
)


class SelectionStateError(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class SelectionStateEncryptionError(SelectionStateError):
    pass


@dataclass(frozen=True)
class SelectionEncryptionContext:
    selection_id: str
    patient_id_hash: str
    origin_message_id: str
    selection_type: str

    def associated_data(self) -> bytes:
        return canonical_json(asdict(self)).encode("utf-8")


class SelectionStateCipher:
    """Encrypt a patient-bound candidate snapshot in the Agent DB."""

    def __init__(self, *, key_id: str, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(
                "selection_state_encryption_key_must_be_32_bytes"
            )
        if not key_id.strip():
            raise ValueError(
                "selection_state_encryption_key_id_required"
            )
        self.key_id = key_id.strip()
        self._key = key
        self._cipher = AESGCM(key)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
    ) -> SelectionStateCipher:
        root_key_id, root_key = (
            settings.require_agent_feedback_encryption()
        )
        derived_key = hmac.new(
            root_key,
            b"agent-food-selection-state-v1",
            hashlib.sha256,
        ).digest()
        return cls(
            key_id=f"{root_key_id}:food-selection-v1",
            key=derived_key,
        )

    def patient_digest(self, patient_id: str) -> str:
        return self._digest(
            b"agent-food-selection-patient-v1\0"
            + patient_id.encode("utf-8")
        )

    def payload_digest(self, payload: dict[str, Any]) -> str:
        return self._digest(
            b"agent-food-selection-payload-v1\0"
            + canonical_json(payload).encode("utf-8")
        )

    def selected_value_digest(self, value: str) -> str:
        return self._digest(
            b"agent-food-selection-value-v1\0"
            + value.encode("utf-8")
        )

    def encrypt_payload(
        self,
        payload: dict[str, Any],
        *,
        context: SelectionEncryptionContext,
    ) -> str:
        nonce = os.urandom(12)
        ciphertext = self._cipher.encrypt(
            nonce,
            canonical_json(payload).encode("utf-8"),
            context.associated_data(),
        )
        return "v1." + base64.urlsafe_b64encode(
            nonce + ciphertext
        ).decode("ascii")

    def decrypt_payload(
        self,
        token: str,
        *,
        context: SelectionEncryptionContext,
        expected_hash: str,
    ) -> dict[str, Any]:
        if not token.startswith("v1."):
            raise SelectionStateEncryptionError(
                "selection_state_ciphertext_version_invalid"
            )
        try:
            encoded = token.removeprefix("v1.")
            raw = base64.urlsafe_b64decode(
                encoded + ("=" * (-len(encoded) % 4))
            )
            if len(raw) < 29:
                raise ValueError("ciphertext_too_short")
            value = json.loads(
                self._cipher.decrypt(
                    raw[:12],
                    raw[12:],
                    context.associated_data(),
                ).decode("utf-8")
            )
            if not isinstance(value, dict):
                raise ValueError("selection_payload_not_object")
            if not hmac.compare_digest(
                self.payload_digest(value),
                expected_hash,
            ):
                raise ValueError(
                    "selection_payload_hash_mismatch"
                )
            return value
        except (
            InvalidTag,
            UnicodeDecodeError,
            ValueError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise SelectionStateEncryptionError(
                "selection_state_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return hmac.new(
            self._key,
            value,
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True)
class PreparedFoodSelection:
    selection_id: str
    origin_message_id: str
    candidate_count: int


@dataclass(frozen=True)
class ResolvedFoodSelection:
    selection_id: str
    origin_message_id: str
    source_chat_request_id: str
    selected_value: str
    candidate: dict[str, Any]
    meal_type: str
    meal_date: str

    def record_arguments(self) -> dict[str, Any]:
        if self.meal_type not in MEAL_TYPES:
            raise SelectionStateError(
                "food_selection_meal_type_required"
            )
        food = {
            key: self.candidate.get(key)
            for key in (
                "food_ref_id",
                "food_name",
                "portion",
                "nutrients",
            )
            if self.candidate.get(key) is not None
        }
        try:
            validated = NutritionMealMutationPayload.model_validate(
                {
                    "meal_type": self.meal_type,
                    "meal_date": date.fromisoformat(
                        self.meal_date
                    ),
                    "foods": [food],
                }
            )
        except (TypeError, ValueError) as exc:
            raise SelectionStateError(
                "food_selection_record_arguments_invalid"
            ) from exc
        return validated.model_dump(
            mode="json",
            exclude_none=True,
        )


class FoodSelectionStateStore:
    """Restart-safe candidate state for a v1.3 string selection box."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        cipher: SelectionStateCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = cipher or SelectionStateCipher.from_settings(
            settings
        )
        self.ttl_seconds = int(
            settings.agent_food_selection_ttl_seconds
        )
        self.retention_seconds = int(
            settings.agent_food_selection_retention_seconds
        )

    def prepare_from_agent_response(
        self,
        *,
        patient_id: str,
        origin_message_id: str,
        source_chat_request_id: str,
        trace_id: str,
        message_at: datetime,
        response: AgentResponse,
    ) -> PreparedFoodSelection | None:
        snapshot = _food_selection_snapshot(
            response,
            message_at=message_at,
        )
        if snapshot is None:
            return None
        required = {
            "patient_id": patient_id,
            "origin_message_id": origin_message_id,
            "source_chat_request_id": source_chat_request_id,
            "trace_id": trace_id,
        }
        missing = [
            key
            for key, value in required.items()
            if not str(value).strip()
        ]
        if missing:
            raise SelectionStateError(
                "food_selection_context_missing:"
                + ",".join(sorted(missing))
            )

        patient_hash = self.cipher.patient_digest(patient_id)
        payload_hash = self.cipher.payload_digest(snapshot)
        now = utc_now()
        with self.session_factory() as session:
            existing = session.scalar(
                select(AgentPendingSelection)
                .where(
                    AgentPendingSelection.origin_message_id
                    == origin_message_id
                )
                .with_for_update()
            )
            if existing is not None:
                if (
                    existing.patient_id_hash != patient_hash
                    or not hmac.compare_digest(
                        existing.payload_hash,
                        payload_hash,
                    )
                ):
                    raise SelectionStateError(
                        "food_selection_origin_message_conflict"
                    )
                return PreparedFoodSelection(
                    selection_id=existing.public_id,
                    origin_message_id=existing.origin_message_id,
                    candidate_count=len(
                        _state_candidates(
                            self._state(existing)
                        )
                    ),
                )

            session.execute(
                update(AgentPendingSelection)
                .where(
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                    AgentPendingSelection.status.in_(
                        ACTIVE_STATUSES
                    ),
                )
                .values(
                    status=SUPERSEDED,
                    resolved_at=now,
                    updated_at=now,
                    version=(
                        AgentPendingSelection.version + 1
                    ),
                )
            )
            selection_id = f"selection_{uuid4().hex}"
            context = SelectionEncryptionContext(
                selection_id=selection_id,
                patient_id_hash=patient_hash,
                origin_message_id=origin_message_id,
                selection_type=NUTRITION_FOOD,
            )
            row = AgentPendingSelection(
                public_id=selection_id,
                patient_id_hash=patient_hash,
                origin_message_id=origin_message_id,
                source_chat_request_id=source_chat_request_id,
                trace_id=trace_id,
                selection_type=NUTRITION_FOOD,
                payload_ciphertext=self.cipher.encrypt_payload(
                    snapshot,
                    context=context,
                ),
                payload_hash=payload_hash,
                encryption_key_id=self.cipher.key_id,
                status=PENDING,
                selected_value_hash="",
                resolved_by_message_id="",
                version=1,
                created_at=now,
                updated_at=now,
                selection_expires_at=now
                + timedelta(seconds=self.ttl_seconds),
                expires_at=now
                + timedelta(seconds=self.retention_seconds),
            )
            session.add(row)
            session.commit()
            return PreparedFoodSelection(
                selection_id=selection_id,
                origin_message_id=origin_message_id,
                candidate_count=len(
                    _state_candidates(snapshot)
                ),
            )

    def resolve(
        self,
        *,
        patient_id: str,
        current_user_message_id: str,
        originating_user_message_id: str,
        submitted_value: str,
    ) -> ResolvedFoodSelection | None:
        patient_hash = self.cipher.patient_digest(patient_id)
        normalized_value = submitted_value.strip()
        if not normalized_value:
            raise SelectionStateError(
                "food_selection_value_required"
            )
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingSelection)
                .where(
                    AgentPendingSelection.origin_message_id
                    == originating_user_message_id,
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                    AgentPendingSelection.selection_type
                    == NUTRITION_FOOD,
                )
                .with_for_update()
            )
            if row is None:
                return None
            if row.status not in ACTIVE_STATUSES:
                return None
            if row.selection_expires_at <= now:
                row.status = EXPIRED
                row.resolved_at = now
                row.updated_at = now
                row.version += 1
                session.commit()
                raise SelectionStateError(
                    "food_selection_state_expired"
                )

            state = self._state(row)
            candidates = _state_candidates(state)
            matches = [
                candidate
                for candidate in candidates
                if str(
                    candidate.get("selection_value") or ""
                ).strip()
                == normalized_value
            ]
            if len(matches) != 1:
                raise SelectionStateError(
                    "food_selection_value_invalid"
                )

            selected_hash = self.cipher.selected_value_digest(
                normalized_value
            )
            if row.status == RESOLVED:
                if (
                    row.resolved_by_message_id
                    != current_user_message_id
                    or not hmac.compare_digest(
                        row.selected_value_hash,
                        selected_hash,
                    )
                ):
                    raise SelectionStateError(
                        "food_selection_idempotency_conflict"
                    )
            else:
                row.status = RESOLVED
                row.selected_value_hash = selected_hash
                row.resolved_by_message_id = (
                    current_user_message_id
                )
                row.resolved_at = now
                row.updated_at = now
                row.version += 1
            session.commit()

            candidate = dict(matches[0])
            candidate.pop("selection_value", None)
            return ResolvedFoodSelection(
                selection_id=row.public_id,
                origin_message_id=row.origin_message_id,
                source_chat_request_id=(
                    row.source_chat_request_id
                ),
                selected_value=normalized_value,
                candidate=candidate,
                meal_type=str(
                    state.get("meal_type") or ""
                ),
                meal_date=str(
                    state.get("meal_date") or ""
                ),
            )

    def consume(
        self,
        *,
        patient_id: str,
        origin_message_id: str,
        current_user_message_id: str,
    ) -> None:
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingSelection)
                .where(
                    AgentPendingSelection.origin_message_id
                    == origin_message_id,
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                )
                .with_for_update()
            )
            if row is None:
                raise SelectionStateError(
                    "food_selection_state_not_found"
                )
            if row.status == CONSUMED:
                if (
                    row.resolved_by_message_id
                    != current_user_message_id
                ):
                    raise SelectionStateError(
                        "food_selection_consume_conflict"
                    )
                return
            if (
                row.status != RESOLVED
                or row.resolved_by_message_id
                != current_user_message_id
            ):
                raise SelectionStateError(
                    "food_selection_state_not_resolved"
                )
            row.status = CONSUMED
            row.updated_at = now
            row.resolved_at = now
            row.version += 1
            session.commit()

    def _state(
        self,
        row: AgentPendingSelection,
    ) -> dict[str, Any]:
        context = SelectionEncryptionContext(
            selection_id=row.public_id,
            patient_id_hash=row.patient_id_hash,
            origin_message_id=row.origin_message_id,
            selection_type=row.selection_type,
        )
        return self.cipher.decrypt_payload(
            row.payload_ciphertext,
            context=context,
            expected_hash=row.payload_hash,
        )


def _food_selection_snapshot(
    response: AgentResponse,
    *,
    message_at: datetime,
) -> dict[str, Any] | None:
    structured = response.structured_payload
    if isinstance(
        structured.get("mutation_confirmation"),
        dict,
    ):
        return None

    raw_candidates = structured.get("food_candidates")
    meal_type = ""
    searches = structured.get("food_searches")
    if isinstance(searches, list):
        for search in reversed(searches):
            if not isinstance(search, dict):
                continue
            candidates = search.get("candidates")
            if (
                isinstance(candidates, list)
                and candidates
            ):
                if not isinstance(raw_candidates, list):
                    raw_candidates = candidates
                meal_type = str(
                    search.get("meal_type") or ""
                ).strip()
                break
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return None

    candidates: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("food_name") or "").strip()
        if not label or label in seen_labels:
            continue
        candidate = dict(raw)
        candidate["food_name"] = label
        candidate["selection_value"] = label
        candidates.append(candidate)
        seen_labels.add(label)
    if not candidates:
        return None
    if meal_type not in MEAL_TYPES:
        meal_type = ""
    return {
        "selection_type": NUTRITION_FOOD,
        "meal_type": meal_type,
        "meal_date": message_at.date().isoformat(),
        "candidates": candidates,
    }


def _state_candidates(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = state.get("candidates")
    if not isinstance(raw, list):
        raise SelectionStateError(
            "food_selection_candidates_missing"
        )
    return [
        dict(candidate)
        for candidate in raw
        if isinstance(candidate, dict)
    ]
