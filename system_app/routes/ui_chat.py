from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.json_utils import dump_json, parse_json_object
from shared.public_ids import AssistantMessageId
from shared.settings import get_settings
from system_app.contracts_v13 import BackendChatRequest
from system_app.models import ChatMessage
from system_app.routes.ui_responses import ui_error_body, ui_success
from system_app.runtime import SystemRuntime
from system_app.services.agent_client import AgentServiceError
from system_app.services.backend_chat_service import (
    BackendChatConflict,
    PendingChatResponseError,
    build_agent_chat_request,
    call_agent_and_persist_response,
    existing_backend_chat_response,
    mark_user_message_failed,
    persist_user_message,
)
from system_app.services.chat_stream import publish_ui_text_with
from system_app.services.clock_service import ensure_clock
from system_app.services.missed_dose_conversation_service import (
    active_missed_dose_conversation_alert,
    missed_dose_conversation_alert_for_message,
)
from system_app.services.missed_dose_reply_understanding import (
    complete_missed_dose_reply,
    missed_dose_reply_request_metadata,
)
from system_app.services.timeline_service import ensure_chat_message_for_conversation_alert
from system_app.ui_contracts import UiChatRequest, UiChatSyncResponse

_SEOUL = ZoneInfo("Asia/Seoul")
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_QUEUE_MAX_EVENTS = 256


def stream_ui_chat(
    runtime: SystemRuntime,
    session: Session,
    payload: UiChatRequest,
    *,
    sync_ui_chat: Callable[..., Awaitable[JSONResponse]],
) -> StreamingResponse:
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=_STREAM_QUEUE_MAX_EVENTS)

    async def publish_text(text: str) -> None:
        await queue.put(("text_delta", text))

    async def run_chat() -> None:
        try:
            with publish_ui_text_with(publish_text):
                response = await sync_ui_chat(runtime, session, payload)
            await queue.put(("response", response))
        except Exception:
            await queue.put(
                (
                    "fatal_error",
                    {
                        "request_id": payload.request_id,
                        "code": "BACKEND_CHAT_PROCESSING_ERROR",
                        "message": ("AI 답변을 처리하지 못했습니다. 대체 응답은 생성하지 않았습니다."),
                        "retryable": True,
                        "details": None,
                    },
                )
            )

    async def event_stream():
        worker = asyncio.create_task(run_chat())
        worker.add_done_callback(_consume_stream_worker_exception)
        sequence = 0
        yield _ui_sse_frame(
            "start",
            {
                "request_id": payload.request_id,
                "status": "processing",
            },
        )
        try:
            while True:
                try:
                    event_type, event_payload = await asyncio.wait_for(
                        queue.get(),
                        timeout=_STREAM_HEARTBEAT_SECONDS,
                    )
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event_type == "text_delta":
                    yield _ui_sse_frame(
                        "text_delta",
                        {
                            "request_id": payload.request_id,
                            "sequence": sequence,
                            "text": event_payload,
                        },
                    )
                    sequence += 1
                    continue
                if event_type == "fatal_error":
                    yield _ui_sse_frame("error", event_payload)
                    return

                response = event_payload
                body = _ui_json_response_body(response)
                if response.status_code == 200:
                    data = body.get("data")
                    if isinstance(data, dict):
                        yield _ui_sse_frame("completed", data)
                    else:
                        yield _ui_sse_frame(
                            "error",
                            {
                                "code": "BACKEND_STREAM_INVALID",
                                "message": "Backend 완료 응답을 확인할 수 없습니다.",
                                "retryable": False,
                                "details": None,
                            },
                        )
                else:
                    error = body.get("error")
                    error_body = (
                        error
                        if isinstance(error, dict)
                        else {
                            "code": "BACKEND_CHAT_PROCESSING_ERROR",
                            "message": ("Backend가 채팅 요청을 처리하지 못했습니다."),
                            "retryable": response.status_code >= 500,
                            "details": None,
                        }
                    )
                    yield _ui_sse_frame(
                        "error",
                        {
                            **error_body,
                            "request_id": payload.request_id,
                        },
                    )
                return
        except asyncio.CancelledError:
            if not worker.done():
                try:
                    await asyncio.shield(worker)
                except Exception:
                    pass
            raise

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


async def sync_ui_chat(
    runtime: SystemRuntime,
    session: Session,
    payload: UiChatRequest,
):
    settings = get_settings()
    clock = ensure_clock(session)
    display_message_at = clock.current_time.replace(tzinfo=_SEOUL)
    backend_payload = BackendChatRequest(
        patient_id=settings.patient_id,
        message=payload.message,
        requested_return_type=payload.requested_return_type,
        source_message_id=(payload.source_message_id if payload.requested_return_type != "text" else None),
        request_id=payload.request_id,
        message_at=display_message_at,
    )
    missed_dose_notification_id: int | None = None
    try:
        with runtime.write_lock:
            user_message, request_id, message_at = persist_user_message(
                session,
                backend_payload,
            )
            display_message_at = message_at
            user_metadata = parse_json_object(user_message.metadata_json)
            requested_source_message_id = payload.source_message_id
            if "ui_source_message_id" in user_metadata:
                if user_metadata.get("ui_source_message_id") != requested_source_message_id:
                    raise BackendChatConflict("IDEMPOTENCY_CONFLICT")
            else:
                user_metadata["ui_source_message_id"] = requested_source_message_id
            existing_missed_dose_context = user_metadata.get("missed_dose_reply")
            if isinstance(existing_missed_dose_context, dict):
                try:
                    missed_dose_notification_id = int(existing_missed_dose_context.get("conversation_alert_id"))
                except (TypeError, ValueError):
                    missed_dose_notification_id = None

            missed_dose_source = None
            if requested_source_message_id:
                missed_dose_source = missed_dose_conversation_alert_for_message(
                    session,
                    requested_source_message_id,
                )
            else:
                notification = active_missed_dose_conversation_alert(
                    session,
                    include_acknowledged=True,
                )
                if notification is not None:
                    prompt_message = ensure_chat_message_for_conversation_alert(
                        session,
                        notification,
                    )
                    if prompt_message is not None:
                        missed_dose_source = (notification, prompt_message)

            if missed_dose_source is not None and "missed_dose_reply" not in user_metadata:
                notification, _prompt_message = missed_dose_source
                missed_dose_notification_id = notification.id
                user_metadata.update(
                    missed_dose_reply_request_metadata(
                        notification,
                    )
                )
            user_message.metadata_json = dump_json(user_metadata)
            session.flush()
            replay = existing_backend_chat_response(
                session,
                request_id=request_id,
            )
            agent_request = build_agent_chat_request(
                user_message=user_message,
                request_id=request_id,
                message_at=message_at,
                requested_return_type=payload.requested_return_type,
            )
            session.commit()
            if replay is not None:
                complete_missed_dose_reply(
                    session,
                    notification_id=missed_dose_notification_id,
                    assistant_message_id=replay.assistant_message_id,
                )
                assistant_sort_sequence = _set_display_message_at(
                    session,
                    assistant_public_id=replay.assistant_message_id,
                    display_message_at=display_message_at.isoformat(),
                )
                session.commit()
                return ui_success(
                    UiChatSyncResponse,
                    {
                        **replay.model_dump(mode="json"),
                        "user_sort_sequence": user_message.id,
                        "assistant_sort_sequence": assistant_sort_sequence,
                        "display_message_at": display_message_at.isoformat(),
                    },
                )
            user_message_id = user_message.id
    except BackendChatConflict:
        session.rollback()
        return JSONResponse(
            status_code=409,
            content=ui_error_body(
                "IDEMPOTENCY_CONFLICT",
                "The request_id was already used with a different chat body.",
            ),
        )
    except PendingChatResponseError as exc:
        session.rollback()
        return JSONResponse(
            status_code=409,
            content=ui_error_body(exc.code, exc.message),
        )
    except Exception:
        session.rollback()
        return JSONResponse(
            status_code=500,
            content=ui_error_body(
                "BACKEND_CHAT_PROCESSING_ERROR",
                "The chat request could not be persisted.",
                retryable=True,
            ),
        )
    try:
        response = await call_agent_and_persist_response(
            session,
            runtime.agent_client,
            request=agent_request,
        )
        with runtime.write_lock:
            complete_missed_dose_reply(
                session,
                notification_id=missed_dose_notification_id,
                assistant_message_id=response.assistant_message_id,
            )
            assistant_sort_sequence = _set_display_message_at(
                session,
                assistant_public_id=response.assistant_message_id,
                display_message_at=display_message_at.isoformat(),
            )
            session.commit()
        return ui_success(
            UiChatSyncResponse,
            {
                **response.model_dump(mode="json"),
                "user_sort_sequence": user_message_id,
                "assistant_sort_sequence": assistant_sort_sequence,
                "display_message_at": display_message_at.isoformat(),
            },
        )
    except AgentServiceError as exc:
        session.rollback()
        with runtime.write_lock:
            mark_user_message_failed(
                session,
                user_message_id=user_message_id,
                error_code=exc.error_type,
                retryable=exc.retryable,
            )
            session.commit()
        return JSONResponse(
            status_code=502,
            content=ui_error_body(
                exc.error_type,
                ("AI Server 연결 또는 답변 생성에 실패했습니다. 대체 응답은 생성하지 않았습니다."),
                retryable=exc.retryable,
            ),
        )
    except Exception:
        session.rollback()
        with runtime.write_lock:
            mark_user_message_failed(
                session,
                user_message_id=user_message_id,
                error_code="BACKEND_CHAT_PROCESSING_ERROR",
                retryable=True,
            )
            session.commit()
        return JSONResponse(
            status_code=500,
            content=ui_error_body(
                "BACKEND_CHAT_PROCESSING_ERROR",
                ("AI 답변을 처리하지 못했습니다. 대체 응답은 생성하지 않았습니다."),
                retryable=True,
            ),
        )


def _ui_sse_frame(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"


def _ui_json_response_body(response: JSONResponse) -> dict[str, Any]:
    try:
        value = json.loads(bytes(response.body))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _consume_stream_worker_exception(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    task.exception()


def _set_display_message_at(
    session: Session,
    *,
    assistant_public_id: AssistantMessageId,
    display_message_at: str,
) -> int:
    assistant = session.scalar(
        select(ChatMessage).where(
            ChatMessage.public_id == assistant_public_id,
            ChatMessage.patient_id == get_settings().patient_id,
            ChatMessage.role == "assistant",
        )
    )
    if assistant is None:
        raise RuntimeError("assistant_message_not_found")
    metadata = parse_json_object(assistant.metadata_json)
    metadata["display_message_at"] = display_message_at
    assistant.metadata_json = dump_json(metadata)
    parsed_display_at = datetime.fromisoformat(display_message_at)
    if parsed_display_at.tzinfo is not None and parsed_display_at.utcoffset() is not None:
        parsed_display_at = parsed_display_at.astimezone(UTC).replace(tzinfo=None)
    else:
        parsed_display_at = parsed_display_at.replace(tzinfo=_SEOUL).astimezone(UTC).replace(tzinfo=None)
    assistant.display_at = parsed_display_at
    assistant.conversation_at = parsed_display_at
    session.flush()
    return assistant.id
