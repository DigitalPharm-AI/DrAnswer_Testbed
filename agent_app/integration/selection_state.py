from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from agent_app.integration.food_selection_payloads import (
    NUTRITION_FOOD,
    build_food_selection_snapshot,
    food_group_candidates,
    food_portion_definitions,
    food_state_candidates,
    food_state_groups,
    format_food_quantity,
    public_food_candidate,
    scale_food_nutrients,
    selected_food_candidates,
)
from agent_app.integration.selection_errors import (
    SelectionStateEncryptionError,
    SelectionStateError,
)
from agent_app.integration.state_crypto import (
    AgentStateCipher,
    AgentStateCipherError,
)
from agent_app.persistence.models import AgentPendingSelection
from shared.backend_v13_contracts import (
    NutritionMealMutationPayload,
)
from shared.json_utils import canonical_json
from shared.nutrition_domain import MEAL_TYPES
from shared.schemas import AgentResponse
from shared.settings import Settings
from shared.time_utils import utc_now

PENDING = "PENDING"
RESOLVED = "RESOLVED"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
SUPERSEDED = "SUPERSEDED"
ACTIVE_STATUSES = (PENDING, RESOLVED)

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
        self._state_cipher = AgentStateCipher(key)

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
        return self._state_cipher.encrypt(
            canonical_json(payload).encode("utf-8"),
            associated_data=context.associated_data(),
        )

    def decrypt_payload(
        self,
        token: str,
        *,
        context: SelectionEncryptionContext,
        expected_hash: str,
    ) -> dict[str, Any]:
        try:
            value = json.loads(
                self._state_cipher.decrypt(
                    token,
                    associated_data=context.associated_data(),
                    version_error=(
                        "selection_state_ciphertext_version_invalid"
                    ),
                    authentication_error=(
                        "selection_state_ciphertext_authentication_failed"
                    ),
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
        except AgentStateCipherError as exc:
            raise SelectionStateEncryptionError(str(exc)) from exc
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise SelectionStateEncryptionError(
                "selection_state_ciphertext_authentication_failed"
            ) from exc

    def _digest(self, value: bytes) -> str:
        return self._state_cipher.digest(value)


@dataclass(frozen=True)
class PreparedFoodSelection:
    selection_id: str
    origin_message_id: str
    candidate_count: int


@dataclass(frozen=True)
class ResolvedFoodSelection:
    kind: Literal["next_selection", "completed"]
    selection_id: str
    origin_message_id: str
    source_chat_request_id: str
    selected_value: str
    candidate: dict[str, Any]
    selected_candidates: list[dict[str, Any]]
    meal_type: str
    meal_date: str
    next_query: str = ""
    next_candidates: list[dict[str, Any]] | None = None
    next_group_number: int = 0
    total_groups: int = 0
    portions_confirmed: bool = False

    def record_arguments(self) -> dict[str, Any]:
        if self.kind != "completed":
            raise SelectionStateError(
                "food_selection_batch_not_completed"
            )
        if self.meal_type not in MEAL_TYPES:
            raise SelectionStateError(
                "food_selection_meal_type_required"
            )
        foods = [
            {
                key: candidate.get(key)
                for key in (
                    "food_ref_id",
                    "food_name",
                    "portion",
                    "nutrients",
                )
                if candidate.get(key) is not None
            }
            for candidate in self.selected_candidates
        ]
        try:
            validated = NutritionMealMutationPayload.model_validate(
                {
                    "meal_type": self.meal_type,
                    "meal_date": date.fromisoformat(
                        self.meal_date
                    ),
                    "foods": foods,
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


def food_selection_question_response(
    transition: ResolvedFoodSelection,
    *,
    trace_id: str,
) -> AgentResponse:
    if transition.kind != "next_selection":
        raise ValueError(
            "food_selection_next_transition_required"
        )
    candidates = transition.next_candidates or []
    selections = [
        str(candidate.get("selection_value") or "").strip()
        for candidate in candidates
        if str(
            candidate.get("selection_value") or ""
        ).strip()
    ]
    if not selections:
        raise SelectionStateError(
            "food_selection_candidates_missing"
        )
    title = (
        "항목 선택 "
        f"({transition.next_group_number}/"
        f"{transition.total_groups})"
    )
    text = (
        f"{transition.next_query}와 가장 가까운 "
        "항목을 선택해 주세요."
    )
    return AgentResponse(
        trace_id=trace_id,
        agent_name="food_selection_state",
        prompt_version_id="food_selection_batch_v1",
        decision_type="food_selection_required",
        structured_payload={
            "routing_mode": (
                "deterministic_food_selection_transition"
            ),
            "executed_by": "food_selection_state",
            "final_answer_source": (
                "deterministic_selection_state"
            ),
            "selection_state_resolution": {
                "reason_code": (
                    "NEXT_FOOD_SELECTION_REQUIRED"
                ),
                "selection_id": transition.selection_id,
                "origin_message_id": (
                    transition.origin_message_id
                ),
                "current_group": (
                    transition.next_group_number
                ),
                "total_groups": transition.total_groups,
                "candidate_reused": True,
                "search_repeated": False,
            },
            "chat_response": {
                "message_type": "selection_box",
                "message": {
                    "message_title": title,
                    "text": text,
                    "tables": None,
                    "selections": selections,
                    "inputs": None,
                },
            },
        },
        human_summary=text,
        requires_conversation_alert=False,
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
        snapshot = build_food_selection_snapshot(
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
                        food_state_candidates(
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
                    food_state_candidates(snapshot)
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
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                    AgentPendingSelection.selection_type
                    == NUTRITION_FOOD,
                    AgentPendingSelection.status.in_(
                        ACTIVE_STATUSES
                    ),
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
            expected_origin = str(
                state.get(
                    "expected_originating_user_message_id"
                )
                or ""
            ).strip()
            if expected_origin != originating_user_message_id:
                raise SelectionStateError(
                    "food_selection_stale_response"
                )
            groups = food_state_groups(state)
            current_index = int(
                state.get("current_index") or 0
            )
            if not 0 <= current_index < len(groups):
                raise SelectionStateError(
                    "food_selection_current_group_invalid"
                )
            current_group = groups[current_index]
            candidates = food_group_candidates(current_group)
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
            candidate = dict(matches[0])
            current_group["selected_value"] = normalized_value
            current_group["selected_candidate"] = candidate
            current_group["resolved_by_message_id"] = (
                current_user_message_id
            )
            groups[current_index] = current_group
            state["groups"] = groups
            state["current_index"] = current_index + 1
            state[
                "expected_originating_user_message_id"
            ] = current_user_message_id

            selected_candidates = (
                selected_food_candidates(groups)
            )
            has_next = current_index + 1 < len(groups)
            row.selected_value_hash = selected_hash
            row.resolved_by_message_id = (
                current_user_message_id
            )
            row.updated_at = now
            row.version += 1
            if has_next:
                row.status = PENDING
                row.resolved_at = None
            else:
                row.status = RESOLVED
                row.resolved_at = now
                state["phase"] = "awaiting_portions"
            self._replace_state(row, state)
            session.commit()

            public_candidate = dict(candidate)
            public_candidate.pop("selection_value", None)
            next_group = (
                groups[current_index + 1]
                if has_next
                else None
            )
            return ResolvedFoodSelection(
                kind=(
                    "next_selection"
                    if has_next
                    else "completed"
                ),
                selection_id=row.public_id,
                origin_message_id=row.origin_message_id,
                source_chat_request_id=(
                    row.source_chat_request_id
                ),
                selected_value=normalized_value,
                candidate=public_candidate,
                selected_candidates=[
                    public_food_candidate(selected)
                    for selected in selected_candidates
                ],
                meal_type=str(
                    state.get("meal_type") or ""
                ),
                meal_date=str(
                    state.get("meal_date") or ""
                ),
                next_query=(
                    str(next_group.get("query") or "")
                    if next_group is not None
                    else ""
                ),
                next_candidates=(
                    food_group_candidates(next_group)
                    if next_group is not None
                    else None
                ),
                next_group_number=(
                    current_index + 2
                    if has_next
                    else 0
                ),
                total_groups=len(groups),
                portions_confirmed=False,
            )

    def resolve_portions(
        self,
        *,
        patient_id: str,
        current_user_message_id: str,
        originating_user_message_id: str,
        submitted_values: dict[
            str,
            str | int | float | bool | None,
        ],
    ) -> ResolvedFoodSelection | None:
        patient_hash = self.cipher.patient_digest(patient_id)
        now = utc_now()
        with self.session_factory() as session:
            row = session.scalar(
                select(AgentPendingSelection)
                .where(
                    AgentPendingSelection.patient_id_hash
                    == patient_hash,
                    AgentPendingSelection.selection_type
                    == NUTRITION_FOOD,
                    AgentPendingSelection.status == RESOLVED,
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
                raise SelectionStateError(
                    "food_selection_state_expired"
                )

            state = self._state(row)
            if state.get("phase") != "awaiting_portions":
                return None
            expected_origin = str(
                state.get(
                    "expected_originating_user_message_id"
                )
                or ""
            ).strip()
            if expected_origin != originating_user_message_id:
                raise SelectionStateError(
                    "food_portion_input_stale_response"
                )

            groups = food_state_groups(state)
            selected_candidates = selected_food_candidates(groups)
            definitions = food_portion_definitions(
                selected_candidates
            )
            expected_labels = {
                definition["label"] for definition in definitions
            }
            if set(submitted_values) != expected_labels:
                raise SelectionStateError(
                    "food_portion_input_fields_mismatch"
                )

            updated_candidates: list[dict[str, Any]] = []
            for definition, candidate in zip(
                definitions,
                selected_candidates,
                strict=True,
            ):
                value = submitted_values[definition["label"]]
                if isinstance(value, bool) or value is None:
                    raise SelectionStateError(
                        "food_portion_input_value_invalid"
                    )
                try:
                    quantity = float(value)
                except (TypeError, ValueError) as exc:
                    raise SelectionStateError(
                        "food_portion_input_value_invalid"
                    ) from exc
                lower = float(definition["lower"])
                upper = float(definition["upper"])
                if not lower <= quantity <= upper:
                    raise SelectionStateError(
                        "food_portion_input_value_out_of_range"
                    )
                updated = dict(candidate)
                updated["portion"] = (
                    f"{format_food_quantity(quantity)}"
                    f"{definition['unit']}"
                )
                reference_quantity = float(
                    definition["reference_quantity"]
                )
                if reference_quantity > 0:
                    updated["nutrients"] = scale_food_nutrients(
                        candidate.get("nutrients"),
                        quantity / reference_quantity,
                    )
                updated_candidates.append(updated)

            candidate_index = 0
            for group in groups:
                if not isinstance(
                    group.get("selected_candidate"),
                    dict,
                ):
                    continue
                group["selected_candidate"] = (
                    updated_candidates[candidate_index]
                )
                candidate_index += 1
            state["groups"] = groups
            state["phase"] = "portions_confirmed"
            state[
                "expected_originating_user_message_id"
            ] = current_user_message_id
            row.resolved_by_message_id = current_user_message_id
            row.resolved_at = now
            row.updated_at = now
            row.version += 1
            self._replace_state(row, state)
            session.commit()

            first_candidate = dict(updated_candidates[0])
            first_candidate.pop("selection_value", None)
            return ResolvedFoodSelection(
                kind="completed",
                selection_id=row.public_id,
                origin_message_id=row.origin_message_id,
                source_chat_request_id=(
                    row.source_chat_request_id
                ),
                selected_value=str(
                    groups[-1].get("selected_value") or ""
                ),
                candidate=first_candidate,
                selected_candidates=[
                    public_food_candidate(candidate)
                    for candidate in updated_candidates
                ],
                meal_type=str(state.get("meal_type") or ""),
                meal_date=str(state.get("meal_date") or ""),
                total_groups=len(groups),
                portions_confirmed=True,
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
