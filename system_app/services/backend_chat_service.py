from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_app.integration.chat_contracts import ChatSyncRequest, ChatSyncResponse
from shared.json_utils import dump_json, parse_json_object
from system_app.contracts_v12 import BackendChatRequest, BackendChatResponse
from system_app.models import ChatMessage
from system_app.services.agent_client import AgentClient


class BackendChatConflict(RuntimeError):
    pass


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

    message = ChatMessage(
        patient_id=payload.patient_id,
        conversation_id=payload.conversation_id,
        ai_request_id=request_id,
        role="user",
        sender_type="patient",
        category="multiturn_chat",
        message_type="text",
        content=payload.message,
        message_payload_json=dump_json({"text": payload.message}),
        processing_status="pending",
        metadata_json=dump_json(
            {
                "message_at": message_at.isoformat(),
                "requested_return_type": payload.requested_return_type,
            }
        ),
        created_at=_naive_utc(message_at),
    )
    session.add(message)
    session.flush()
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
            "user_message_id": str(user.id),
            "assistant_message_id": str(assistant.id),
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
        message_id=str(user_message.id),
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
    user_message = session.get(ChatMessage, int(request.message_id))
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
    )
    assistant = ChatMessage(
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
        metadata_json=dump_json({"message_at": response.message_at.isoformat()}),
        created_at=_naive_utc(response.message_at),
    )
    session.add(assistant)
    user_message.processing_status = "completed"
    session.flush()
    return BackendChatResponse(
        request_id=response.request_id,
        conversation_id=user_message.conversation_id,
        user_message_id=str(user_message.id),
        assistant_message_id=str(assistant.id),
        message_type=response.message_type,
        message=response.message,
        message_at=response.message_at,
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
