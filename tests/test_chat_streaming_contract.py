from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.responses import JSONResponse
from agent_app.integration.idempotency import (
    StoredHttpResponse,
    SyncRequestClaim,
    canonical_request_hash,
)
from agent_app.routes import chat as agent_chat_route
from agent_app.streaming import agent_text_publisher
from shared.chat_contracts import (
    ChatMessageContent,
    ChatStreamEvent,
    ChatSyncRequest,
)
from shared.schemas import AgentResponse
from system_app.routes import ui_api
from system_app.services import agent_client as agent_client_module
from system_app.services.agent_client import AgentClient, AgentServiceError
from system_app.services.chat_stream import publish_ui_text_with
from system_app.ui_contracts import UiChatRequest


def _ndjson_rows(body: str) -> list[dict]:
    return [json.loads(line) for line in body.splitlines() if line]


def _sse_rows(body: str) -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    for frame in body.strip().split("\n\n"):
        event = ""
        data = ""
        for line in frame.splitlines():
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data = line.split(":", 1)[1].strip()
        if event and data:
            rows.append((event, json.loads(data)))
    return rows


async def _stream_body(response) -> str:
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(
            chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        )
    return "".join(chunks)


def _patch_httpx_stream(monkeypatch, response: httpx.Response) -> None:
    class _StreamContext:
        async def __aenter__(self):
            return response

        async def __aexit__(self, *_args):
            return False

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return _StreamContext()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())


def _patch_httpx_stream_sequence(
    monkeypatch,
    responses: list[httpx.Response],
    captured_payloads: list[dict],
) -> None:
    pending = list(responses)

    class _StreamContext:
        def __init__(self, response: httpx.Response) -> None:
            self.response = response

        async def __aenter__(self):
            return self.response

        async def __aexit__(self, *_args):
            return False

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **kwargs):
            if not pending:
                raise AssertionError("unexpected additional Agent retry")
            captured_payloads.append(dict(kwargs["json"]))
            return _StreamContext(pending.pop(0))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _Client())


class _Gate:
    def __init__(self) -> None:
        self.completed: list[dict] = []
        self.failed: list[dict] = []

    def complete(self, _payload, **kwargs) -> None:
        self.completed.append(kwargs)

    def fail(self, _payload, **kwargs) -> None:
        self.failed.append(kwargs)


class _TraceStore:
    def __init__(self) -> None:
        self.completed: list[AgentResponse] = []
        self.failed: list[dict] = []

    def complete_chat(
        self,
        _payload,
        response: AgentResponse,
        **_kwargs,
    ) -> None:
        self.completed.append(response)

    def fail_chat(self, **kwargs) -> None:
        self.failed.append(kwargs)


def _request(suffix: str = "1") -> ChatSyncRequest:
    token = hashlib.sha256(suffix.encode("utf-8")).hexdigest()[:16]
    return ChatSyncRequest(
        request_id=f"req_{token}",
        message_id=f"user_msg_{token}",
        patient_id=f"patient_{token}",
        requested_return_type="text",
        message="질문",
        message_at=datetime.now(UTC),
    )


def _claim(request: ChatSyncRequest) -> SyncRequestClaim:
    return SyncRequestClaim(
        api_path="/agent/sync/chat",
        request_id=request.request_id,
        request_hash=canonical_request_hash(request),
        attempt_epoch=1,
    )


def _stream_event(
    request: ChatSyncRequest,
    *,
    sequence: int,
    status: str,
    delta: str | None = None,
    text: str | None = None,
    error: dict | None = None,
) -> dict:
    message = None
    message_type = None
    if status == "streaming":
        message_type = "text"
    elif status == "completed":
        message_type = "text"
        message = {
            "message_title": None,
            "text": text,
            "tables": None,
            "selections": None,
            "inputs": None,
        }
    return {
        "request_id": request.request_id,
        "message_id": request.message_id,
        "sequence": sequence,
        "status": status,
        "message_type": message_type,
        "delta": delta,
        "message": message,
        "error": error,
        "event_at": datetime.now(UTC).isoformat(),
    }


async def test_agent_ndjson_emits_agent_loop_text_and_one_terminal_event():
    request = _request()

    class _Orchestrator:
        async def invoke(self, _kind, _payload, *, trace_id):
            return AgentResponse(
                trace_id=trace_id,
                agent_name="multiturn_chat_agent",
                prompt_version_id="test",
                decision_type="system_guidance",
                structured_payload={},
                human_summary="최종 응답",
            )

    gate = _Gate()
    trace_store = _TraceStore()
    response = agent_chat_route._stream_chat_response(
        gate=gate,
        trace_store=trace_store,
        orchestrator=_Orchestrator(),
        payload=request,
        claim=_claim(request),
        agent_payload={"message": request.message},
        trace_id="trace-1",
        timeout_seconds=1.0,
    )
    body = await _stream_body(response)
    rows = [
        ChatStreamEvent.model_validate(row)
        for row in _ndjson_rows(body)
    ]

    assert response.headers["content-type"].startswith(
        "application/x-ndjson"
    )
    assert [row.status for row in rows] == [
        "streaming",
        "completed",
    ]
    assert [row.sequence for row in rows] == [0, 1]
    assert "".join(
        row.delta or "" for row in rows if row.status == "streaming"
    ) == "최종 응답"
    assert rows[-1].message is not None
    assert rows[-1].message.text == "최종 응답"
    assert sum(row.status in {"completed", "error"} for row in rows) == 1
    assert len(gate.completed) == 1
    assert gate.failed == []
    assert len(trace_store.completed) == 1


async def test_agent_ndjson_forwards_generated_token_deltas_without_duplicate():
    request = _request("token-stream")

    class _Orchestrator:
        async def invoke(self, _kind, _payload, *, trace_id):
            publisher = agent_text_publisher()
            assert publisher is not None
            await publisher("토큰 ")
            await publisher("스트리밍")
            return AgentResponse(
                trace_id=trace_id,
                agent_name="multiturn_chat_agent",
                prompt_version_id="test",
                decision_type="system_guidance",
                structured_payload={},
                human_summary="토큰 스트리밍",
            )

    response = agent_chat_route._stream_chat_response(
        gate=_Gate(),
        trace_store=_TraceStore(),
        orchestrator=_Orchestrator(),
        payload=request,
        claim=_claim(request),
        agent_payload={"message": request.message},
        trace_id="trace-token-stream",
        timeout_seconds=1.0,
    )
    rows = [
        ChatStreamEvent.model_validate(row)
        for row in _ndjson_rows(await _stream_body(response))
    ]

    assert [row.status for row in rows] == [
        "streaming",
        "streaming",
        "completed",
    ]
    assert [row.sequence for row in rows] == [0, 1, 2]
    assert [
        row.delta for row in rows if row.status == "streaming"
    ] == ["토큰 ", "스트리밍"]
    assert rows[-1].message is not None
    assert rows[-1].message.text == "토큰 스트리밍"


async def test_structured_chat_streams_explanation_before_completed_payload():
    request = _request("structured")

    class _Orchestrator:
        async def invoke(self, _kind, _payload, *, trace_id):
            return AgentResponse(
                trace_id=trace_id,
                agent_name="multiturn_chat_agent",
                prompt_version_id="test",
                decision_type="system_guidance",
                human_summary="하나를 선택해 주세요.",
                structured_payload={
                    "selection_box": {
                        "message_title": "후보 선택",
                        "text": "하나를 선택해 주세요.",
                        "tables": None,
                        "selections": ["첫 번째", "두 번째"],
                        "inputs": None,
                    }
                },
            )

    response = agent_chat_route._stream_chat_response(
        gate=_Gate(),
        trace_store=_TraceStore(),
        orchestrator=_Orchestrator(),
        payload=request,
        claim=_claim(request),
        agent_payload={"message": request.message},
        trace_id="trace-structured",
        timeout_seconds=1.0,
    )
    rows = _ndjson_rows(await _stream_body(response))

    assert len(rows) == 2
    explanation = ChatStreamEvent.model_validate(rows[0])
    terminal = ChatStreamEvent.model_validate(rows[1])
    assert explanation.sequence == 0
    assert explanation.status == "streaming"
    assert explanation.message_type == "text"
    assert explanation.delta == "하나를 선택해 주세요."
    assert explanation.message is None
    assert terminal.sequence == 1
    assert terminal.status == "completed"
    assert terminal.message_type == "selection_box"
    assert terminal.message is not None
    assert terminal.message.text == "하나를 선택해 주세요."
    assert terminal.message.selections == ["첫 번째", "두 번째"]
    assert sum(
        row.status in {"completed", "error"}
        for row in (explanation, terminal)
    ) == 1


async def test_agent_ndjson_timeout_is_http_200_error_terminal():
    request = _request("timeout")
    gate = _Gate()
    trace_store = _TraceStore()

    class _Orchestrator:
        async def invoke(self, _kind, _payload, *, trace_id):
            await asyncio.sleep(0.05)
            raise AssertionError(trace_id)

    response = agent_chat_route._stream_chat_response(
        gate=gate,
        trace_store=trace_store,
        orchestrator=_Orchestrator(),
        payload=request,
        claim=_claim(request),
        agent_payload={"message": request.message},
        trace_id="trace-timeout",
        timeout_seconds=0.001,
    )
    rows = _ndjson_rows(await _stream_body(response))

    assert response.status_code == 200
    assert len(rows) == 1
    terminal = ChatStreamEvent.model_validate(rows[0])
    assert terminal.sequence == 0
    assert terminal.status == "error"
    assert terminal.error is not None
    assert terminal.error.code == "AI_PROCESSING_TIMEOUT"
    assert terminal.error.retryable is True
    assert gate.completed == []
    assert len(gate.failed) == 1
    assert len(trace_store.failed) == 1


async def test_terminal_replay_is_one_ndjson_line_with_sequence_zero():
    request = _request("replay")
    terminal = ChatStreamEvent(
        request_id=request.request_id,
        message_id=request.message_id,
        sequence=7,
        status="completed",
        message_type="text",
        delta=None,
        message=ChatMessageContent(
            message_title=None,
            text="저장된 답변",
            tables=None,
            selections=None,
            inputs=None,
        ),
        error=None,
        event_at=datetime.now(UTC),
    )
    response = agent_chat_route._replay_chat_response(
        request,
        StoredHttpResponse(
            status_code=200,
            body=terminal.model_dump(mode="json"),
        ),
    )
    rows = _ndjson_rows(await _stream_body(response))

    assert len(rows) == 1
    replay = ChatStreamEvent.model_validate(rows[0])
    assert replay.sequence == 0
    assert replay.status == "completed"
    assert replay.message is not None
    assert replay.message.text == "저장된 답변"


async def test_backend_agent_client_consumes_ndjson_and_relays_only_deltas(
    monkeypatch,
):
    request = _request("client")
    events = [
        _stream_event(
            request,
            sequence=0,
            status="streaming",
            delta="나눠진 ",
        ),
        _stream_event(
            request,
            sequence=1,
            status="streaming",
            delta="응답",
        ),
        _stream_event(
            request,
            sequence=2,
            status="completed",
            text="나눠진 응답",
        ),
    ]
    body = "".join(
        json.dumps(event, ensure_ascii=False) + "\n"
        for event in events
    )
    response = httpx.Response(
        200,
        request=httpx.Request(
            "POST",
            "http://agent.test/agent/sync/chat",
        ),
        headers={"Content-Type": "application/x-ndjson"},
        content=body.encode("utf-8"),
    )

    _patch_httpx_stream(monkeypatch, response)
    client = object.__new__(AgentClient)
    client.base_url = "http://agent.test"
    client.agent_sync_api_token = "token"
    published: list[str] = []

    async def publish(text: str) -> None:
        published.append(text)

    with publish_ui_text_with(publish):
        result = await client.send_sync_chat(request)

    assert published == ["나눠진 ", "응답"]
    assert result.message.text == "나눠진 응답"


async def test_backend_agent_client_retries_retryable_terminal_without_delta(
    monkeypatch,
):
    request = _request("retry-terminal")
    retryable_error = _stream_event(
        request,
        sequence=0,
        status="error",
        error={
            "code": "LLM_PROVIDER_REQUEST_FAILED",
            "message": "일시적인 생성 장애",
            "retryable": True,
            "details": None,
        },
    )
    completed = _stream_event(
        request,
        sequence=0,
        status="completed",
        text="재시도 완료",
    )
    responses = [
        httpx.Response(
            200,
            request=httpx.Request(
                "POST",
                "http://agent.test/agent/sync/chat",
            ),
            headers={"Content-Type": "application/x-ndjson"},
            content=(
                json.dumps(retryable_error, ensure_ascii=False) + "\n"
            ).encode("utf-8"),
        ),
        httpx.Response(
            200,
            request=httpx.Request(
                "POST",
                "http://agent.test/agent/sync/chat",
            ),
            headers={"Content-Type": "application/x-ndjson"},
            content=(
                json.dumps(completed, ensure_ascii=False) + "\n"
            ).encode("utf-8"),
        ),
    ]
    captured_payloads: list[dict] = []
    _patch_httpx_stream_sequence(
        monkeypatch,
        responses,
        captured_payloads,
    )
    settings = agent_client_module.get_settings().model_copy(
        update={
            "agent_sync_max_retries": 1,
            "agent_sync_chat_timeout_seconds": 5.0,
            "agent_sync_chat_total_timeout_seconds": 20.0,
        }
    )

    async def allow_retry(_deadline: float, _delay: float) -> bool:
        return True

    monkeypatch.setattr(
        agent_client_module,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        agent_client_module,
        "_wait_for_retry",
        allow_retry,
    )
    client = object.__new__(AgentClient)
    client.base_url = "http://agent.test"
    client.agent_sync_api_token = "token"
    published: list[str] = []

    async def publish(text: str) -> None:
        published.append(text)

    with publish_ui_text_with(publish):
        result = await client.send_sync_chat(request)

    assert result.message.text == "재시도 완료"
    assert published == []
    assert len(captured_payloads) == 2
    assert captured_payloads[0] == captured_payloads[1]
    assert captured_payloads[0]["request_id"] == request.request_id


async def test_backend_agent_client_does_not_retry_after_public_delta(
    monkeypatch,
):
    request = _request("retry-after-delta")
    events = [
        _stream_event(
            request,
            sequence=0,
            status="streaming",
            delta="이미 공개된 응답",
        ),
        _stream_event(
            request,
            sequence=1,
            status="error",
            error={
                "code": "LLM_PROVIDER_REQUEST_FAILED",
                "message": "일시적인 생성 장애",
                "retryable": True,
                "details": None,
            },
        ),
    ]
    response = httpx.Response(
        200,
        request=httpx.Request(
            "POST",
            "http://agent.test/agent/sync/chat",
        ),
        headers={"Content-Type": "application/x-ndjson"},
        content=(
            "".join(
                json.dumps(event, ensure_ascii=False) + "\n"
                for event in events
            )
        ).encode("utf-8"),
    )
    captured_payloads: list[dict] = []
    _patch_httpx_stream_sequence(
        monkeypatch,
        [response],
        captured_payloads,
    )
    settings = agent_client_module.get_settings().model_copy(
        update={
            "agent_sync_max_retries": 1,
            "agent_sync_chat_timeout_seconds": 5.0,
            "agent_sync_chat_total_timeout_seconds": 20.0,
        }
    )

    async def fail_if_retry_waited(
        _deadline: float,
        _delay: float,
    ) -> bool:
        raise AssertionError("public delta must prevent automatic retry")

    monkeypatch.setattr(
        agent_client_module,
        "get_settings",
        lambda: settings,
    )
    monkeypatch.setattr(
        agent_client_module,
        "_wait_for_retry",
        fail_if_retry_waited,
    )
    client = object.__new__(AgentClient)
    client.base_url = "http://agent.test"
    client.agent_sync_api_token = "token"
    published: list[str] = []

    async def publish(text: str) -> None:
        published.append(text)

    with publish_ui_text_with(publish):
        with pytest.raises(AgentServiceError) as exc_info:
            await client.send_sync_chat(request)

    assert exc_info.value.error_type == "LLM_PROVIDER_REQUEST_FAILED"
    assert exc_info.value.retryable is True
    assert published == ["이미 공개된 응답"]
    assert len(captured_payloads) == 1


@pytest.mark.parametrize(
    ("events", "expected_error_type"),
    [
        (
            [
                {
                    **_stream_event(
                        _request("invalid"),
                        sequence=0,
                        status="completed",
                        text="응답",
                    ),
                    "request_id": "req_00000000deadbeef",
                }
            ],
            "agent_response_correlation_mismatch",
        ),
        (
            [
                _stream_event(
                    _request("invalid"),
                    sequence=1,
                    status="streaming",
                    delta="잘못된 순서",
                )
            ],
            "agent_stream_invalid",
        ),
        (
            [
                {
                    **_stream_event(
                        _request("invalid"),
                        sequence=0,
                        status="error",
                        error={
                            "code": "AI_PROCESSING_ERROR",
                            "message": "실패",
                            "retryable": True,
                            "details": None,
                        },
                    ),
                    "message_id": "user_msg_00000000deadbeef",
                }
            ],
            "agent_response_correlation_mismatch",
        ),
        (
            [
                _stream_event(
                    _request("invalid"),
                    sequence=0,
                    status="completed",
                    text="응답",
                ),
                _stream_event(
                    _request("invalid"),
                    sequence=1,
                    status="completed",
                    text="추가 응답",
                ),
            ],
            "agent_stream_invalid",
        ),
    ],
)
async def test_backend_agent_client_rejects_invalid_ndjson(
    monkeypatch,
    events,
    expected_error_type,
):
    request = _request("invalid")
    body = "".join(
        json.dumps(event, ensure_ascii=False) + "\n"
        for event in events
    )
    response = httpx.Response(
        200,
        request=httpx.Request(
            "POST",
            "http://agent.test/agent/sync/chat",
        ),
        headers={"Content-Type": "application/x-ndjson"},
        content=body.encode("utf-8"),
    )
    _patch_httpx_stream(monkeypatch, response)
    client = object.__new__(AgentClient)
    client.base_url = "http://agent.test"
    client.agent_sync_api_token = "token"

    async def publish(_text: str) -> None:
        raise AssertionError("invalid stream text must not be published")

    with publish_ui_text_with(publish):
        with pytest.raises(AgentServiceError) as exc_info:
            await client.send_sync_chat(request)

    assert exc_info.value.error_type == expected_error_type


async def test_ui_sse_remains_the_browser_boundary_and_starts_at_sequence_zero(
    monkeypatch,
):
    payload = UiChatRequest(
        request_id="req_0000000000000001",
        message="질문",
        requested_return_type="text",
        source_message_id=None,
    )
    completed = {
        "request_id": "req_0000000000000001",
        "user_message_id": "user_msg_0000000000000001",
        "user_sort_sequence": 1,
        "assistant_message_id": "assistant_msg_0000000000000001",
        "assistant_sort_sequence": 2,
        "message_type": "text",
        "message": {
            "message_title": None,
            "text": "백엔드 응답",
            "tables": None,
            "selections": None,
            "inputs": None,
        },
        "message_at": datetime.now(UTC).isoformat(),
        "display_message_at": datetime.now(UTC).isoformat(),
    }

    async def fake_sync_chat(_runtime, _session, _payload):
        from system_app.services.chat_stream import ui_text_publisher

        publisher = ui_text_publisher()
        assert publisher is not None
        await publisher("백엔드 ")
        await publisher("응답")
        return JSONResponse(
            status_code=200,
            content={"success": True, "data": completed, "error": None},
        )

    monkeypatch.setattr(ui_api, "_sync_ui_chat", fake_sync_chat)

    response = ui_api._stream_ui_chat(object(), object(), payload)
    body = await _stream_body(response)
    rows = _sse_rows(body)

    assert response.media_type == "text/event-stream"
    assert [event for event, _data in rows] == [
        "start",
        "text_delta",
        "text_delta",
        "completed",
    ]
    deltas = [data for event, data in rows if event == "text_delta"]
    assert [delta["sequence"] for delta in deltas] == [0, 1]
    assert rows[-1][1] == completed
