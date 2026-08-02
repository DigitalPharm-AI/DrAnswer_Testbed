from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage

from agent_app.llm.messages import (
    build_chat_messages,
    public_text_delta_from_ai_message,
    public_text_from_ai_message,
)
from agent_app.observability.model_calls import (
    capture_model_calls,
    traced_model_astream_message,
)
from agent_app.providers.deterministic_test import (
    DeterministicTestProvider,
)
from agent_app.streaming import publish_agent_text_with


class _TokenStreamingModel:
    async def astream(self, _messages):
        yield AIMessageChunk(content="토큰 ")
        yield AIMessageChunk(content="단위 ")
        yield AIMessageChunk(content="응답")


class _ToolStreamingModel:
    async def astream(self, _messages):
        yield AIMessageChunk(
            content="",
            tool_call_chunks=[
                {
                    "name": "lookup",
                    "args": "{}",
                    "id": "call-1",
                    "index": 0,
                    "type": "tool_call_chunk",
                }
            ],
        )


def test_streamed_model_message_is_the_authoritative_final_message() -> None:
    deltas: list[str] = []
    observations: list[dict] = []

    async def publish(delta: str) -> None:
        deltas.append(delta)

    async def run():
        with (
            capture_model_calls(observations),
            publish_agent_text_with(publish),
        ):
            return await traced_model_astream_message(
                _TokenStreamingModel(),
                [HumanMessage(content="질문")],
                name="test.final",
                prompt_version_id="test",
                publish_public_text=True,
            )

    message = asyncio.run(run())

    assert deltas == ["토큰 ", "단위 ", "응답"]
    assert public_text_from_ai_message(message) == "토큰 단위 응답"
    assert len(observations) == 1
    assert observations[0]["status"] == "COMPLETED"
    assert observations[0]["time_to_first_token_ms"] >= 0


def test_public_stream_delta_excludes_private_reasoning_blocks() -> None:
    chunk = AIMessageChunk(
        content=[
            {
                "type": "reasoning_content",
                "reasoning_content": {"text": "비공개 추론"},
            },
            {"type": "text", "text": "공개 답변"},
        ]
    )

    assert public_text_delta_from_ai_message(chunk) == "공개 답변"


def test_tool_call_stream_is_not_published_as_user_text() -> None:
    deltas: list[str] = []

    async def publish(delta: str) -> None:
        deltas.append(delta)

    async def run():
        with publish_agent_text_with(publish):
            return await traced_model_astream_message(
                _ToolStreamingModel(),
                [HumanMessage(content="조회")],
                name="test.tool",
                prompt_version_id="test",
                publish_public_text=True,
            )

    message = asyncio.run(run())

    assert deltas == []
    assert message.tool_calls == [
        {
            "name": "lookup",
            "args": {},
            "id": "call-1",
            "type": "tool_call",
        }
    ]


def test_deterministic_provider_can_fail_after_public_token(
    monkeypatch,
) -> None:
    marker = "[stream-failure-after-token]"
    monkeypatch.setenv(
        "DETERMINISTIC_TEST_STREAM_FAILURE_MARKER",
        marker,
    )
    deltas: list[str] = []
    observations: list[dict] = []

    async def publish(delta: str) -> None:
        deltas.append(delta)

    async def run() -> None:
        with (
            capture_model_calls(observations),
            publish_agent_text_with(publish),
        ):
            await traced_model_astream_message(
                DeterministicTestProvider().chat_model(),
                build_chat_messages(
                    "test",
                    {
                        "message": f"부분 응답 테스트 {marker}",
                        "decision_type": "system_guidance",
                    },
                ),
                name="test.failure_after_token",
                prompt_version_id="test",
                publish_public_text=True,
            )

    with pytest.raises(
        RuntimeError,
        match="deterministic_test_stream_failure_after_token",
    ):
        asyncio.run(run())

    assert deltas == ["테스트 부분 응답"]
    assert len(observations) == 1
    assert observations[0]["status"] == "ERROR"


def test_deterministic_provider_can_emit_burst_token_stream(
    monkeypatch,
) -> None:
    marker = "[burst-token-stream]"
    monkeypatch.setenv(
        "DETERMINISTIC_TEST_BURST_STREAM_MARKER",
        marker,
    )
    monkeypatch.setenv(
        "DETERMINISTIC_TEST_BURST_CHUNK_COUNT",
        "4",
    )
    monkeypatch.setenv(
        "DETERMINISTIC_TEST_BURST_CHUNK_SIZE",
        "32",
    )
    deltas: list[str] = []

    async def publish(delta: str) -> None:
        deltas.append(delta)

    async def run():
        with publish_agent_text_with(publish):
            return await traced_model_astream_message(
                DeterministicTestProvider().chat_model(),
                build_chat_messages(
                    "test",
                    {
                        "message": f"대량 토큰 테스트 {marker}",
                        "decision_type": "system_guidance",
                    },
                ),
                name="test.burst_stream",
                prompt_version_id="test",
                publish_public_text=True,
            )

    message = asyncio.run(run())

    assert len(deltas) == 4
    assert all(len(delta) == 32 for delta in deltas)
    assert [delta[:5] for delta in deltas] == [
        "0000|",
        "0001|",
        "0002|",
        "0003|",
    ]
    assert public_text_from_ai_message(message) == "".join(deltas)
