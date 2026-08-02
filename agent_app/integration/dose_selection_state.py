from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.selection_errors import (
    DoseSelectionStateEncryptionError,
    DoseSelectionStateError,
    SelectionStateEncryptionError,
)
from agent_app.integration.selection_state import (
    ACTIVE_STATUSES,
    CONSUMED,
    EXPIRED,
    PENDING,
    RESOLVED,
    SUPERSEDED,
    SelectionEncryptionContext,
    SelectionStateCipher,
)
from agent_app.persistence.models import AgentPendingSelection
from shared.schemas import AgentResponse
from shared.settings import Settings
from shared.time_utils import utc_now

MEDICATION_DOSE_SELECTION = "medication_dose"


class DoseSelectionStateCipher(SelectionStateCipher):
    """Domain-separated encryption for trusted dose candidates."""

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
    ) -> DoseSelectionStateCipher:
        root_key_id, root_key = (
            settings.require_agent_feedback_encryption()
        )
        derived_key = hmac.new(
            root_key,
            b"agent-medication-dose-selection-state-v1",
            hashlib.sha256,
        ).digest()
        return cls(
            key_id=f"{root_key_id}:medication-dose-selection-v1",
            key=derived_key,
        )

    def patient_digest(self, patient_id: str) -> str:
        return self._digest(
            b"agent-medication-dose-selection-patient-v1\0"
            + patient_id.encode("utf-8")
        )

    def payload_digest(self, payload: dict[str, Any]) -> str:
        from shared.json_utils import canonical_json

        return self._digest(
            b"agent-medication-dose-selection-payload-v1\0"
            + canonical_json(payload).encode("utf-8")
        )

    def selected_value_digest(self, value: str) -> str:
        return self._digest(
            b"agent-medication-dose-selection-value-v1\0"
            + value.encode("utf-8")
        )


@dataclass(frozen=True)
class PreparedDoseSelection:
    selection_id: str
    origin_message_id: str
    candidate_count: int


@dataclass(frozen=True)
class ResolvedDoseSelection:
    selection_id: str
    origin_message_id: str
    source_chat_request_id: str
    selected_value: str
    dose_event_id: str
    candidate: dict[str, Any]


class DoseSelectionStateStore:
    """Restart-safe mapping from a displayed label to a trusted dose ID."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        settings: Settings,
        cipher: DoseSelectionStateCipher | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.cipher = (
            cipher
            or DoseSelectionStateCipher.from_settings(settings)
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
    ) -> PreparedDoseSelection | None:
        del message_at
        snapshot = _dose_selection_snapshot(response)
        if snapshot is None:
            return None
        required = {
            "patient_id": patient_id,
            "origin_message_id": origin_message_id,
            "source_chat_request_id": source_chat_request_id,
            "trace_id": trace_id,
        }
        missing = sorted(
            key
            for key, value in required.items()
            if not str(value).strip()
        )
        if missing:
            raise DoseSelectionStateError(
                "dose_selection_context_missing:"
                + ",".join(missing)
            )
        snapshot["expected_originating_user_message_id"] = (
            origin_message_id
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
                    existing.selection_type
                    != MEDICATION_DOSE_SELECTION
                    or existing.patient_id_hash != patient_hash
                    or not hmac.compare_digest(
                        existing.payload_hash,
                        payload_hash,
                    )
                ):
                    raise DoseSelectionStateError(
                        "dose_selection_origin_message_conflict"
                    )
                return PreparedDoseSelection(
                    selection_id=existing.public_id,
                    origin_message_id=existing.origin_message_id,
                    candidate_count=len(snapshot["candidates"]),
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
                    version=AgentPendingSelection.version + 1,
                )
            )
            selection_id = f"selection_{uuid4().hex}"
            context = SelectionEncryptionContext(
                selection_id=selection_id,
                patient_id_hash=patient_hash,
                origin_message_id=origin_message_id,
                selection_type=MEDICATION_DOSE_SELECTION,
            )
            row = AgentPendingSelection(
                public_id=selection_id,
                patient_id_hash=patient_hash,
                origin_message_id=origin_message_id,
                source_chat_request_id=source_chat_request_id,
                trace_id=trace_id,
                selection_type=MEDICATION_DOSE_SELECTION,
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
            return PreparedDoseSelection(
                selection_id=selection_id,
                origin_message_id=origin_message_id,
                candidate_count=len(snapshot["candidates"]),
            )

    def resolve(
        self,
        *,
        patient_id: str,
        current_user_message_id: str,
        originating_user_message_id: str,
        submitted_value: str,
    ) -> ResolvedDoseSelection | None:
        normalized_value = submitted_value.strip()
        if not normalized_value:
            raise DoseSelectionStateError(
                "dose_selection_value_required"
            )
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingSelection)
                .where(
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                    AgentPendingSelection.selection_type
                    == MEDICATION_DOSE_SELECTION,
                    AgentPendingSelection.status.in_(
                        ACTIVE_STATUSES
                    ),
                )
                .with_for_update()
            )
            if row is None:
                return None
            if row.selection_expires_at <= now:
                row.status = EXPIRED
                row.resolved_at = now
                row.updated_at = now
                row.version += 1
                session.commit()
                raise DoseSelectionStateError(
                    "dose_selection_state_expired"
                )
            state = self._state(row)
            expected_origin = str(
                state.get(
                    "expected_originating_user_message_id"
                )
                or ""
            ).strip()
            if expected_origin != originating_user_message_id:
                raise DoseSelectionStateError(
                    "dose_selection_stale_response"
                )
            if row.status == RESOLVED:
                if row.resolved_by_message_id != current_user_message_id:
                    raise DoseSelectionStateError(
                        "dose_selection_resolution_conflict"
                    )
                candidate = state.get("selected_candidate")
                if not isinstance(candidate, dict):
                    raise DoseSelectionStateError(
                        "dose_selection_resolved_candidate_missing"
                    )
                return _resolved(row, normalized_value, candidate)

            candidates = state.get("candidates")
            matches = [
                candidate
                for candidate in (
                    candidates if isinstance(candidates, list) else []
                )
                if isinstance(candidate, dict)
                and str(
                    candidate.get("selection_value") or ""
                ).strip()
                == normalized_value
            ]
            if len(matches) != 1:
                raise DoseSelectionStateError(
                    "dose_selection_value_invalid"
                )
            candidate = dict(matches[0])
            state["selected_candidate"] = candidate
            row.selected_value_hash = (
                self.cipher.selected_value_digest(normalized_value)
            )
            row.resolved_by_message_id = current_user_message_id
            row.status = RESOLVED
            row.resolved_at = now
            row.updated_at = now
            row.version += 1
            self._replace_state(row, state)
            session.commit()
            return _resolved(row, normalized_value, candidate)

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
                    AgentPendingSelection.selection_type
                    == MEDICATION_DOSE_SELECTION,
                )
                .with_for_update()
            )
            if row is None:
                raise DoseSelectionStateError(
                    "dose_selection_state_not_found"
                )
            if row.status == CONSUMED:
                if row.resolved_by_message_id != current_user_message_id:
                    raise DoseSelectionStateError(
                        "dose_selection_consume_conflict"
                    )
                return
            if (
                row.status != RESOLVED
                or row.resolved_by_message_id
                != current_user_message_id
            ):
                raise DoseSelectionStateError(
                    "dose_selection_state_not_resolved"
                )
            row.status = CONSUMED
            row.resolved_at = now
            row.updated_at = now
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
        try:
            return self.cipher.decrypt_payload(
                row.payload_ciphertext,
                context=context,
                expected_hash=row.payload_hash,
            )
        except SelectionStateEncryptionError as exc:
            raise DoseSelectionStateEncryptionError(
                exc.code
            ) from exc

    def _replace_state(
        self,
        row: AgentPendingSelection,
        state: dict[str, Any],
    ) -> None:
        context = SelectionEncryptionContext(
            selection_id=row.public_id,
            patient_id_hash=row.patient_id_hash,
            origin_message_id=row.origin_message_id,
            selection_type=row.selection_type,
        )
        row.payload_hash = self.cipher.payload_digest(state)
        row.payload_ciphertext = self.cipher.encrypt_payload(
            state,
            context=context,
        )
        row.encryption_key_id = self.cipher.key_id


def _dose_selection_snapshot(
    response: AgentResponse,
) -> dict[str, Any] | None:
    raw = response.structured_payload.get("dose_selection")
    if not isinstance(raw, dict):
        return None
    candidates_raw = raw.get("candidates")
    if not isinstance(candidates_raw, list):
        raise DoseSelectionStateError(
            "dose_selection_candidates_invalid"
        )
    candidates: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    seen_ids: set[str] = set()
    for item in candidates_raw:
        if not isinstance(item, dict):
            raise DoseSelectionStateError(
                "dose_selection_candidate_invalid"
            )
        label = str(item.get("selection_value") or "").strip()
        dose_event_id = str(
            item.get("dose_event_id") or ""
        ).strip()
        if (
            not label
            or not dose_event_id
            or label in seen_labels
            or dose_event_id in seen_ids
        ):
            raise DoseSelectionStateError(
                "dose_selection_candidate_invalid"
            )
        seen_labels.add(label)
        seen_ids.add(dose_event_id)
        candidates.append(dict(item))
    if len(candidates) < 2:
        raise DoseSelectionStateError(
            "dose_selection_requires_multiple_candidates"
        )
    request = response.structured_payload.get("selection_request")
    selections = (
        request.get("selections")
        if isinstance(request, dict)
        else None
    )
    if selections != [
        candidate["selection_value"]
        for candidate in candidates
    ]:
        raise DoseSelectionStateError(
            "dose_selection_display_candidate_mismatch"
        )
    return {
        "action_name": str(raw.get("action_name") or ""),
        "candidates": candidates,
    }


def _resolved(
    row: AgentPendingSelection,
    selected_value: str,
    candidate: dict[str, Any],
) -> ResolvedDoseSelection:
    dose_event_id = str(
        candidate.get("dose_event_id") or ""
    ).strip()
    if not dose_event_id:
        raise DoseSelectionStateError(
            "dose_selection_dose_event_id_missing"
        )
    public_candidate = dict(candidate)
    public_candidate.pop("selection_value", None)
    return ResolvedDoseSelection(
        selection_id=row.public_id,
        origin_message_id=row.origin_message_id,
        source_chat_request_id=row.source_chat_request_id,
        selected_value=selected_value,
        dose_event_id=dose_event_id,
        candidate=public_candidate,
    )
