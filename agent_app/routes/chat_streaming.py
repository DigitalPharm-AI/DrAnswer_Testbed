from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

from fastapi.responses import JSONResponse, Response, StreamingResponse

from agent_app import trace_logging
from agent_app.errors import AgentExecutionError, public_processing_error
from agent_app.integration.idempotency import StoredHttpResponse, SyncRequestClaim, SyncRequestGate
from agent_app.observability.model_calls import capture_model_calls, response_with_model_calls
from agent_app.observability.tool_calls import capture_tool_calls
from agent_app.orchestration.graph import AgentLangGraphNativeOrchestrator
from agent_app.persistence.trace_store import AgentTraceStore
from agent_app.streaming import publish_agent_text_with
from shared.chat_contracts import ChatContractError, ChatStreamEvent, ChatSyncRequest, ChatSyncResponse
from shared.redaction import safe_exception_summary
from shared.schemas import AgentResponse

NDJSON_MEDIA_TYPE = "application/x-ndjson"
STORED_STREAM_EVENTS_KEY = "_chat_stream_events_v1"


def _completed_stream_event(
    payload: ChatSyncRequest,
    response: ChatSyncResponse,
) -> ChatStreamEvent:
    return ChatStreamEvent(
        request_id=payload.request_id,
        message_id=payload.message_id,
        sequence=0,
        status="completed",
        message_type=response.message_type,
        delta=None,
        message=response.message,
        error=None,
        event_at=response.message_at,
    )


def _persist_completed_stream(
    *,
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    payload: ChatSyncRequest,
    claim: SyncRequestClaim,
    response: AgentResponse,
    terminal: ChatStreamEvent,
    published_events: list[ChatStreamEvent],
    run_started: float,
    model_call_observations: list[dict[str, Any]],
    tool_call_observations: list[dict[str, Any]],
    log_sync_chat_completed: Callable[..., None],
) -> None:
    trace_store.complete_chat(
        payload,
        response,
        model_call_observations=model_call_observations,
        tool_call_observations=tool_call_observations,
    )
    stored_events = [event.model_copy(update={"sequence": sequence}).model_dump(mode="json") for sequence, event in enumerate([*published_events, terminal])]
    gate.complete(
        payload,
        claim=claim,
        status_code=200,
        body={STORED_STREAM_EVENTS_KEY: stored_events},
        trace_id=response.trace_id,
    )
    log_sync_chat_completed(
        payload=payload,
        response=response,
        elapsed_ms=max(
            0,
            round((perf_counter() - run_started) * 1000),
        ),
        model_call_observations=model_call_observations,
        tool_call_observations=tool_call_observations,
    )


def _stream_terminal_from_exception(
    payload: ChatSyncRequest,
    exc: Exception,
    *,
    trace_id: str,
) -> ChatStreamEvent:
    if isinstance(exc, AgentExecutionError):
        trace_logging.log_info(
            "agent_sync_chat_failed",
            request_id=payload.request_id,
            trace_id=exc.trace_id,
            error_type=exc.error_type,
            error=safe_exception_summary(exc, limit=300),
        )
        public_error = public_processing_error(exc)
    elif isinstance(exc, TimeoutError):
        public_error = public_processing_error(exc)
    else:
        trace_logging.log_info(
            "agent_sync_chat_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
        public_error = public_processing_error(exc)
    return _stream_error_event(
        payload,
        code=public_error.code,
        message=public_error.message,
        retryable=public_error.retryable,
    )


def stream_chat_response(
    *,
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    orchestrator: AgentLangGraphNativeOrchestrator,
    payload: ChatSyncRequest,
    claim: SyncRequestClaim,
    agent_payload: dict[str, Any],
    trace_id: str,
    timeout_seconds: float,
    invoke_sync_chat_contract: Callable[..., Awaitable[tuple[AgentResponse, ChatSyncResponse]]],
    log_sync_chat_completed: Callable[..., None],
) -> StreamingResponse:
    queue: asyncio.Queue[ChatStreamEvent] = asyncio.Queue()
    client_connected = True
    public_text_published = False
    published_events: list[ChatStreamEvent] = []

    async def publish_text(text: str) -> None:
        nonlocal public_text_published
        if not text:
            return
        public_text_published = True
        event = ChatStreamEvent(
            request_id=payload.request_id,
            message_id=payload.message_id,
            sequence=0,
            status="streaming",
            message_type="text",
            delta=text,
            message=None,
            error=None,
            event_at=_event_time(),
        )
        published_events.append(event)
        if client_connected:
            await queue.put(event)

    async def run_chat() -> None:
        model_call_observations: list[dict[str, Any]] = []
        tool_call_observations: list[dict[str, Any]] = []
        run_started = perf_counter()
        try:
            with (
                capture_model_calls(model_call_observations),
                capture_tool_calls(tool_call_observations),
                publish_agent_text_with(publish_text),
            ):
                agent_response, external_response = await asyncio.wait_for(
                    invoke_sync_chat_contract(
                        orchestrator=orchestrator,
                        agent_payload=agent_payload,
                        payload=payload,
                        trace_id=trace_id,
                    ),
                    timeout=timeout_seconds,
                )
            agent_response = response_with_model_calls(
                agent_response,
                model_call_observations,
            )
            if not public_text_published and external_response.message.text:
                # The normal Agent loop now owns the final answer. Publish that
                # single authoritative text before the completed response/card.
                await publish_text(external_response.message.text)
            terminal = _completed_stream_event(
                payload,
                external_response,
            )
            try:
                _persist_completed_stream(
                    gate=gate,
                    trace_store=trace_store,
                    payload=payload,
                    claim=claim,
                    response=agent_response,
                    terminal=terminal,
                    published_events=published_events,
                    run_started=run_started,
                    model_call_observations=model_call_observations,
                    tool_call_observations=tool_call_observations,
                    log_sync_chat_completed=log_sync_chat_completed,
                )
            except Exception as exc:
                trace_logging.log_info(
                    "agent_sync_chat_completion_persistence_failed",
                    request_id=payload.request_id,
                    trace_id=trace_id,
                    error=safe_exception_summary(exc, limit=300),
                )
                terminal = _stream_error_event(
                    payload,
                    code="AI_PROCESSING_ERROR",
                    message=("An internal AI Server processing error occurred."),
                    retryable=True,
                )
                _persist_stream_failure(
                    gate,
                    trace_store,
                    payload,
                    claim=claim,
                    terminal=terminal,
                    trace_id=trace_id,
                    model_call_observations=model_call_observations,
                    tool_call_observations=tool_call_observations,
                )
        except Exception as exc:
            terminal = _stream_terminal_from_exception(
                payload,
                exc,
                trace_id=trace_id,
            )
            _persist_stream_failure(
                gate,
                trace_store,
                payload,
                claim=claim,
                terminal=terminal,
                trace_id=trace_id,
                model_call_observations=model_call_observations,
                tool_call_observations=tool_call_observations,
            )
        if client_connected:
            await queue.put(terminal)

    async def event_stream() -> AsyncIterator[str]:
        nonlocal client_connected
        worker = asyncio.create_task(run_chat())
        sequence = 0
        try:
            while True:
                event = await queue.get()
                outbound = event.model_copy(update={"sequence": sequence})
                sequence += 1
                yield _ndjson_line(outbound)
                if outbound.status in {"completed", "error"}:
                    await worker
                    return
        except asyncio.CancelledError:
            client_connected = False
            try:
                await asyncio.shield(worker)
            except Exception:
                pass
            raise
        finally:
            client_connected = False
            if worker.done() and not worker.cancelled():
                worker.exception()

    return _ndjson_streaming_response(event_stream())


def _persist_stream_failure(
    gate: SyncRequestGate,
    trace_store: AgentTraceStore,
    payload: ChatSyncRequest,
    *,
    claim: SyncRequestClaim,
    terminal: ChatStreamEvent,
    trace_id: str,
    model_call_observations: list[dict[str, Any]] | None = None,
    tool_call_observations: list[dict[str, Any]] | None = None,
) -> None:
    error = terminal.error
    if error is None:
        raise ValueError("stream_failure_requires_error")
    try:
        trace_store.record_model_calls(
            trace_id=trace_id,
            observations=model_call_observations or [],
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_model_observation_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
    try:
        trace_store.fail_chat(
            trace_id=trace_id,
            error_code=error.code,
            error_message=error.message,
            retryable=error.retryable,
            tool_call_observations=tool_call_observations or [],
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_trace_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )
    try:
        gate.fail(
            payload,
            claim=claim,
            status_code=200,
            body=terminal.model_copy(update={"sequence": 0}).model_dump(mode="json"),
            trace_id=trace_id,
            error_code=error.code,
            retryable=error.retryable,
        )
    except Exception as exc:
        trace_logging.log_info(
            "agent_sync_request_failure_persistence_failed",
            request_id=payload.request_id,
            trace_id=trace_id,
            error=safe_exception_summary(exc, limit=300),
        )


def _stream_error_event(
    payload: ChatSyncRequest,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> ChatStreamEvent:
    return ChatStreamEvent(
        request_id=payload.request_id,
        message_id=payload.message_id,
        sequence=0,
        status="error",
        message_type=None,
        delta=None,
        message=None,
        error=ChatContractError(
            code=code,
            message=message,
            retryable=retryable,
            details=None,
        ),
        event_at=_event_time(),
    )


def replay_chat_response(
    payload: ChatSyncRequest,
    replay: StoredHttpResponse,
) -> Response:
    if replay.status_code != 200:
        return JSONResponse(
            status_code=replay.status_code,
            content=replay.body,
        )
    events = _stored_stream_events(payload, replay.body)

    async def replay_stream() -> AsyncIterator[str]:
        for event in events:
            yield _ndjson_line(event)

    return _ndjson_streaming_response(replay_stream())


def _stored_stream_events(
    payload: ChatSyncRequest,
    body: dict[str, Any],
) -> list[ChatStreamEvent]:
    raw_events = body.get(STORED_STREAM_EVENTS_KEY)
    if raw_events is None:
        return [_stored_terminal_event(payload, body).model_copy(update={"sequence": 0})]
    if not isinstance(raw_events, list) or not raw_events:
        raise RuntimeError("stored_chat_stream_events_invalid")

    events = [ChatStreamEvent.model_validate(raw_event) for raw_event in raw_events]
    for sequence, event in enumerate(events):
        if event.request_id != payload.request_id or event.message_id != payload.message_id or event.sequence != sequence:
            raise RuntimeError("stored_chat_response_correlation_mismatch")
        if sequence < len(events) - 1 and event.status in {
            "completed",
            "error",
        }:
            raise RuntimeError("stored_chat_stream_terminal_not_last")
    if events[-1].status not in {"completed", "error"}:
        raise RuntimeError("stored_chat_stream_terminal_missing")
    return events


def _stored_terminal_event(
    payload: ChatSyncRequest,
    body: dict[str, Any],
) -> ChatStreamEvent:
    terminal = ChatStreamEvent.model_validate(body)
    if terminal.request_id != payload.request_id or terminal.message_id != payload.message_id:
        raise RuntimeError("stored_chat_response_correlation_mismatch")
    return terminal


def accepts_ndjson(value: str | None) -> bool:
    accepted = {item.split(";", 1)[0].strip().lower() for item in str(value or "").split(",") if item.strip()}
    return NDJSON_MEDIA_TYPE in accepted


def _ndjson_streaming_response(
    content: AsyncIterator[str],
) -> StreamingResponse:
    return StreamingResponse(
        content,
        status_code=200,
        headers={
            "Content-Type": (f"{NDJSON_MEDIA_TYPE}; charset=utf-8"),
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _ndjson_line(event: ChatStreamEvent) -> str:
    return (
        json.dumps(
            event.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )


def _event_time() -> datetime:
    return datetime.now(UTC)
