from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from shared.chat_contracts import (
    ChatSyncRequest,
    ChatSyncResponse,
    parse_input_box_message,
)
from shared.json_utils import dump_json, parse_json_object
from system_app.contracts_v12 import BackendChatRequest, BackendChatResponse
from system_app.models import ChatMessage, new_message_public_id
from system_app.services.agent_client import AgentClient


class BackendChatConflict(RuntimeError):
    pass


class PendingChatResponseError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def persist_user_message(
    session: Session,
    payload: BackendChatRequest,
) -> tuple[ChatMessage, str, datetime]:
    request_id = payload.request_id or f"chat-{uuid4()}"
    message_at = payload.message_at or datetime.now(UTC)
    existing = session.scalar(
        select(ChatMessage).where(
            ChatMessage.ai_request_id == request_id,
            ChatMessage.role == "user",
        )
    )
    if existing is not None:
        metadata = parse_json_object(existing.metadata_json)
        if (
            existing.patient_id != payload.patient_id
            or existing.conversation_id != payload.conversation_id
            or existing.content != payload.message
            or metadata.get("requested_return_type") != payload.requested_return_type
        ):
            raise BackendChatConflict("IDEMPOTENCY_CONFLICT")
        persisted_at = _aware_from_metadata(metadata.get("message_at")) or message_at
        return existing, request_id, persisted_at

    pending_response = pending_response_for_submission(
        session,
        patient_id=payload.patient_id,
        conversation_id=payload.conversation_id,
        requested_return_type=payload.requested_return_type,
        message=payload.message,
    )
    metadata = {
        "message_at": message_at.isoformat(),
        "requested_return_type": payload.requested_return_type,
    }
    if pending_response is not None:
        metadata["source_chat_request_id"] = pending_response.ai_request_id
        metadata["source_chat_message_id"] = pending_response.public_id

    message = ChatMessage(
        public_id=new_message_public_id("user"),
        patient_id=payload.patient_id,
        conversation_id=payload.conversation_id,
        ai_request_id=request_id,
        role="user",
        sender_type="patient",
        category="multiturn_chat",
        message_type=payload.requested_return_type or "text",
        content=payload.message,
        message_payload_json=dump_json({"text": payload.message}),
        processing_status="pending",
        metadata_json=dump_json(metadata),
        created_at=_naive_utc(message_at),
    )
    session.add(message)
    session.flush()
    if pending_response is not None:
        mark_pending_response_answered(
            pending_response,
            response_message_id=message.public_id,
            response_request_id=request_id,
        )
    return message, request_id, message_at


def existing_backend_chat_response(
    session: Session,
    *,
    request_id: str,
) -> BackendChatResponse | None:
    assistant = session.scalar(
        select(ChatMessage).where(
            ChatMessage.ai_request_id == request_id,
            ChatMessage.role == "assistant",
        )
    )
    if assistant is None:
        return None
    user = session.get(ChatMessage, assistant.reply_to_message_id)
    if user is None:
        raise RuntimeError("assistant_reply_target_missing")
    payload = parse_json_object(assistant.message_payload_json)
    return BackendChatResponse.model_validate(
        {
            "request_id": request_id,
            "conversation_id": assistant.conversation_id,
            "user_message_id": user.public_id,
            "assistant_message_id": assistant.public_id,
            "message_type": assistant.message_type,
            "message": payload,
            "message_at": _response_time(assistant),
        }
    )


def build_agent_chat_request(
    *,
    user_message: ChatMessage,
    request_id: str,
    message_at: datetime,
    requested_return_type: str | None,
) -> ChatSyncRequest:
    return ChatSyncRequest(
        request_id=request_id,
        message_id=user_message.public_id,
        conversation_id=user_message.conversation_id,
        patient_id=user_message.patient_id,
        requested_return_type=requested_return_type,
        message=user_message.content,
        message_at=message_at,
    )


async def call_agent_and_persist_response(
    session: Session,
    agent_client: AgentClient,
    *,
    request: ChatSyncRequest,
) -> BackendChatResponse:
    response = await agent_client.send_sync_chat(request)
    if response.request_id != request.request_id or response.message_id != request.message_id:
        raise RuntimeError("agent_chat_identifier_mismatch")
    user_message = _message_for_agent_response(session, request)
    if user_message is None:
        raise RuntimeError("backend_user_message_missing_after_agent_response")
    return persist_assistant_response(
        session,
        user_message=user_message,
        response=response,
    )


def persist_assistant_response(
    session: Session,
    *,
    user_message: ChatMessage,
    response: ChatSyncResponse,
) -> BackendChatResponse:
    existing = existing_backend_chat_response(session, request_id=response.request_id)
    if existing is not None:
        return existing

    content = (
        response.message.text
        or response.message.message_title
        or "\n".join(response.message.selections or [])
        or "\n".join(input_item.label for input_item in response.message.inputs or [])
        or ""
    )
    response_metadata = {"message_at": response.message_at.isoformat()}
    if response.message_type in {"selection_box", "input_box"}:
        _supersede_pending_responses(
            session,
            patient_id=user_message.patient_id,
            conversation_id=user_message.conversation_id,
        )
        response_metadata.update(
            {
                "pending_response_type": response.message_type,
                "pending_response_status": "pending",
            }
        )
    assistant = ChatMessage(
        public_id=new_message_public_id("assistant"),
        patient_id=user_message.patient_id,
        conversation_id=user_message.conversation_id,
        ai_request_id=response.request_id,
        role="assistant",
        sender_type="assistant",
        category="multiturn_chat",
        message_type=response.message_type,
        content=content,
        message_payload_json=dump_json(response.message.model_dump(mode="json")),
        reply_to_message_id=user_message.id,
        processing_status="completed",
        metadata_json=dump_json(response_metadata),
        created_at=_naive_utc(response.message_at),
    )
    session.add(assistant)
    user_message.processing_status = "completed"
    session.flush()
    return BackendChatResponse(
        request_id=response.request_id,
        conversation_id=user_message.conversation_id,
        user_message_id=user_message.public_id,
        assistant_message_id=assistant.public_id,
        message_type=response.message_type,
        message=response.message,
        message_at=response.message_at,
    )


def pending_response_for_submission(
    session: Session,
    *,
    patient_id: str,
    conversation_id: str,
    requested_return_type: str | None,
    message: str,
    source_chat_request_id: str | None = None,
) -> ChatMessage | None:
    pending_response = _latest_pending_response(
        session,
        patient_id=patient_id,
        conversation_id=conversation_id,
    )
    if source_chat_request_id:
        source_response = session.scalar(
            select(ChatMessage).where(
                ChatMessage.patient_id == patient_id,
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.ai_request_id == source_chat_request_id,
                ChatMessage.role == "assistant",
            )
        )
        if source_response is None:
            raise PendingChatResponseError(
                "PENDING_RESPONSE_SOURCE_NOT_FOUND",
                "The structured response source was not found in this conversation.",
            )
        source_metadata = parse_json_object(source_response.metadata_json)
        source_status = source_metadata.get("pending_response_status")
        if source_status == "answered":
            raise PendingChatResponseError(
                "PENDING_RESPONSE_ALREADY_ANSWERED",
                "The structured response was already answered.",
            )
        if source_status != "pending":
            raise PendingChatResponseError(
                "PENDING_RESPONSE_NOT_FOUND",
                "The source message is not waiting for a structured response.",
            )
        if pending_response is None or pending_response.id != source_response.id:
            raise PendingChatResponseError(
                "PENDING_RESPONSE_STALE",
                "A newer structured response is waiting in this conversation.",
            )
        pending_response = source_response

    if pending_response is None:
        if requested_return_type is None:
            return None
        raise PendingChatResponseError(
            "PENDING_RESPONSE_NOT_FOUND",
            "No structured response is waiting in this conversation.",
        )

    metadata = parse_json_object(pending_response.metadata_json)
    expected_type = str(metadata.get("pending_response_type") or "")
    if requested_return_type != expected_type:
        raise PendingChatResponseError(
            "REQUESTED_RETURN_TYPE_MISMATCH",
            f"Expected {expected_type or 'no structured response'}, got {requested_return_type or 'text'}.",
        )
    _validate_pending_response_payload(
        pending_response,
        requested_return_type=expected_type,
        message=message,
    )
    return pending_response


def mark_pending_response_answered(
    pending_response: ChatMessage,
    *,
    response_message_id: str,
    response_request_id: str,
) -> None:
    metadata = parse_json_object(pending_response.metadata_json)
    metadata.update(
        {
            "pending_response_status": "answered",
            "response_message_id": response_message_id,
            "response_request_id": response_request_id,
        }
    )
    pending_response.metadata_json = dump_json(metadata)


def _latest_pending_response(
    session: Session,
    *,
    patient_id: str,
    conversation_id: str,
) -> ChatMessage | None:
    rows = session.scalars(
        select(ChatMessage)
        .where(
            ChatMessage.patient_id == patient_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "assistant",
            ChatMessage.message_type.in_(("selection_box", "input_box")),
        )
        .order_by(desc(ChatMessage.created_at), desc(ChatMessage.id))
    ).all()
    for row in rows:
        if parse_json_object(row.metadata_json).get("pending_response_status") == "pending":
            return row
    return None


def _supersede_pending_responses(
    session: Session,
    *,
    patient_id: str,
    conversation_id: str,
) -> None:
    rows = session.scalars(
        select(ChatMessage).where(
            ChatMessage.patient_id == patient_id,
            ChatMessage.conversation_id == conversation_id,
            ChatMessage.role == "assistant",
            ChatMessage.message_type.in_(("selection_box", "input_box")),
        )
    ).all()
    for row in rows:
        metadata = parse_json_object(row.metadata_json)
        if metadata.get("pending_response_status") != "pending":
            continue
        metadata["pending_response_status"] = "superseded"
        row.metadata_json = dump_json(metadata)


def _validate_pending_response_payload(
    pending_response: ChatMessage,
    *,
    requested_return_type: str,
    message: str,
) -> None:
    payload = parse_json_object(pending_response.message_payload_json)
    if requested_return_type == "selection_box":
        selections = payload.get("selections")
        if not isinstance(selections, list) or message.strip() not in selections:
            raise PendingChatResponseError(
                "SELECTION_VALUE_INVALID",
                "The selected value is not one of the source message options.",
            )
        return

    try:
        submitted = parse_input_box_message(message)
    except ValueError as exc:
        raise PendingChatResponseError(
            "INPUT_BOX_MESSAGE_INVALID",
            str(exc),
        ) from exc
    inputs = payload.get("inputs")
    if not isinstance(inputs, list) or not inputs:
        raise PendingChatResponseError(
            "INPUT_BOX_SOURCE_INVALID",
            "The source message does not contain input definitions.",
        )
    definitions: dict[str, dict] = {}
    for item in inputs:
        if not isinstance(item, dict):
            raise PendingChatResponseError(
                "INPUT_BOX_SOURCE_INVALID",
                "The source message contains an invalid input definition.",
            )
        label = str(item.get("label") or "").strip()
        if not label or label in definitions:
            raise PendingChatResponseError(
                "INPUT_BOX_SOURCE_INVALID",
                "The source message contains invalid or duplicate input labels.",
            )
        definitions[label] = item
    if set(submitted) != set(definitions):
        raise PendingChatResponseError(
            "INPUT_BOX_FIELDS_MISMATCH",
            "The submitted input fields do not match the source message.",
        )
    for label, definition in definitions.items():
        _validate_input_value(label, submitted[label], definition)


def _validate_input_value(label: str, value, definition: dict) -> None:
    input_type = definition.get("type")
    options = definition.get("options")
    if not isinstance(options, dict):
        raise PendingChatResponseError(
            "INPUT_BOX_SOURCE_INVALID",
            f"The input definition for {label} has invalid options.",
        )
    if input_type == "dropdown":
        selections = options.get("selections")
        if not isinstance(selections, list) or str(value) not in selections:
            raise PendingChatResponseError(
                "INPUT_BOX_VALUE_INVALID",
                f"The submitted value for {label} is not an allowed selection.",
            )
        return
    if input_type != "number" or isinstance(value, bool):
        raise PendingChatResponseError(
            "INPUT_BOX_VALUE_INVALID",
            f"The submitted value for {label} does not match its input type.",
        )
    try:
        numeric_value = float(value)
        lower = float(options["lower"])
        upper = float(options["upper"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PendingChatResponseError(
            "INPUT_BOX_VALUE_INVALID",
            f"The submitted value for {label} is not numeric.",
        ) from exc
    if not math.isfinite(numeric_value) or numeric_value < lower or numeric_value > upper:
        raise PendingChatResponseError(
            "INPUT_BOX_VALUE_INVALID",
            f"The submitted value for {label} is outside the allowed range.",
        )


def mark_user_message_failed(
    session: Session,
    *,
    user_message_id: int,
    error_code: str,
    retryable: bool,
) -> None:
    row = session.get(ChatMessage, user_message_id)
    if row is None:
        return
    row.processing_status = "retryable_failed" if retryable else "final_failed"
    metadata = parse_json_object(row.metadata_json)
    metadata.update({"error_code": error_code, "retryable": retryable})
    row.metadata_json = dump_json(metadata)
    session.flush()


def _message_for_agent_response(
    session: Session,
    request: ChatSyncRequest,
) -> ChatMessage | None:
    predicates = (
        ChatMessage.patient_id == request.patient_id,
        ChatMessage.conversation_id == request.conversation_id,
        ChatMessage.ai_request_id == request.request_id,
        ChatMessage.role == "user",
    )
    message = session.scalar(
        select(ChatMessage).where(ChatMessage.public_id == request.message_id, *predicates)
    )
    if message is not None:
        return message
    legacy_id = _legacy_positive_id(request.message_id)
    if legacy_id is None:
        return None
    return session.scalar(
        select(ChatMessage).where(ChatMessage.id == legacy_id, *predicates)
    )


def _legacy_positive_id(value: str) -> int | None:
    normalized = str(value or "").strip()
    if not normalized.isascii() or not normalized.isdigit():
        return None
    parsed = int(normalized)
    return parsed if parsed > 0 else None


def _response_time(message: ChatMessage) -> datetime:
    metadata = parse_json_object(message.metadata_json)
    parsed = _aware_from_metadata(metadata.get("message_at"))
    if parsed is not None:
        return parsed
    return message.created_at.replace(tzinfo=UTC)


def _aware_from_metadata(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)
