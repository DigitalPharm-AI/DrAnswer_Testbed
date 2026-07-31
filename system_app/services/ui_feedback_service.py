from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.public_ids import AssistantMessageId, RequestId
from shared.settings import Settings, get_settings
from system_app.contracts_ui_feedback import UiChatOpinionRequest
from system_app.models import ChatMessage


class UiFeedbackTargetNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedFeedbackTarget:
    message_id: AssistantMessageId
    patient_id: str


def resolve_feedback_target(
    session: Session,
    *,
    assistant_message_id: AssistantMessageId,
    settings: Settings | None = None,
) -> ResolvedFeedbackTarget:
    resolved_settings = settings or get_settings()
    target = session.scalar(
        select(ChatMessage).where(
            ChatMessage.public_id == assistant_message_id,
            ChatMessage.patient_id == resolved_settings.patient_id,
            ChatMessage.role == "assistant",
        )
    )
    if target is None:
        raise UiFeedbackTargetNotFound("assistant_message_not_found")
    return ResolvedFeedbackTarget(
        message_id=target.public_id,
        patient_id=target.patient_id,
    )


def build_agent_feedback_payload(
    request: UiChatOpinionRequest,
    target: ResolvedFeedbackTarget,
) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "message_id": target.message_id,
        "patient_id": target.patient_id,
        "reaction": request.reaction,
        "feedback_text": request.opinion_text,
        "feedback_at": request.feedback_at.isoformat(),
    }


def mark_feedback_submitted(
    session: Session,
    *,
    assistant_message_id: AssistantMessageId,
    request_id: RequestId,
    requested_reaction: str | None,
    opinion_included: bool,
    agent_accepted_at: datetime | None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Persist only non-sensitive UI state after Agent acceptance.

    Agent acceptance time is the ordering authority for reactions. This keeps a
    replay of an older, valid request from reverting a newer reaction stored in
    Backend metadata. Opinion-only requests never write reaction metadata.
    """

    resolved_settings = settings or get_settings()
    target = session.scalar(
        select(ChatMessage).where(
            ChatMessage.public_id == assistant_message_id,
            ChatMessage.patient_id == resolved_settings.patient_id,
            ChatMessage.role == "assistant",
        )
    )
    if target is None:
        raise UiFeedbackTargetNotFound("assistant_message_not_found")

    metadata = _metadata_object(target.metadata_json)
    accepted_at = _utc_datetime(agent_accepted_at)

    if requested_reaction is not None:
        if accepted_at is None:
            raise ValueError("reaction_acceptance_time_required")
        current_accepted_at = _metadata_datetime(
            metadata.get("feedback_reaction_accepted_at")
        )
        if (
            current_accepted_at is None
            or accepted_at > current_accepted_at
        ):
            metadata.update(
                {
                    "feedback_reaction": requested_reaction,
                    "feedback_reaction_request_id": request_id,
                    "feedback_reaction_accepted_at": accepted_at.isoformat(),
                }
            )

    if opinion_included:
        existing_request_id = str(
            metadata.get("opinion_request_id") or ""
        )
        existing_submitted_at = _metadata_datetime(
            metadata.get("opinion_submitted_at")
        )
        opinion_accepted_at = accepted_at
        if opinion_accepted_at is None and existing_submitted_at is None:
            opinion_accepted_at = datetime.now(UTC)
        if (
            not metadata.get("opinion_submitted")
            or existing_submitted_at is None
            or (
                opinion_accepted_at is not None
                and opinion_accepted_at > existing_submitted_at
            )
            or (
                existing_request_id == request_id
                and existing_submitted_at is not None
            )
        ):
            submitted_at = (
                existing_submitted_at
                if (
                    existing_request_id == request_id
                    and existing_submitted_at is not None
                )
                else opinion_accepted_at or datetime.now(UTC)
            )
            metadata.update(
                {
                    "opinion_submitted": True,
                    "opinion_submitted_at": submitted_at.isoformat(),
                    "opinion_request_id": request_id,
                }
            )

    target.metadata_json = json.dumps(
        metadata,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    session.flush()
    return feedback_status_from_metadata(target.metadata_json)


def feedback_status_from_metadata(
    metadata_json: str | None,
) -> dict[str, Any]:
    metadata = _metadata_object(metadata_json)
    submitted_at = str(metadata.get("opinion_submitted_at") or "")
    reaction = metadata.get("feedback_reaction")
    if reaction not in {"like", "dislike"}:
        reaction = None
    return {
        "reaction": reaction,
        "opinion_submitted": bool(
            metadata.get("opinion_submitted") and submitted_at
        ),
        "opinion_submitted_at": submitted_at or None,
    }


def _metadata_object(metadata_json: str | None) -> dict[str, Any]:
    try:
        metadata = json.loads(metadata_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _metadata_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _utc_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
