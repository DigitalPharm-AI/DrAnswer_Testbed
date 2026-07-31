from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent_app.jobs import worker


@pytest.mark.asyncio
async def test_async_agent_invocation_times_out_with_terminal_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowOrchestrator:
        async def invoke(
            self,
            _task_type,
            _payload,
            *,
            trace_id=None,
        ):
            await asyncio.sleep(1)
            raise AssertionError("unreachable")

    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(llm_timeout_seconds=0.01),
    )

    with pytest.raises(
        worker.AgentGenerationTimeoutError,
        match=r"llm_generation_timeout:0\.1s",
    ) as captured:
        await worker._invoke_agent_with_timeout(
            SlowOrchestrator(),
            "daily_pattern",
            {},
            trace_id="timeout-trace",
        )

    assert captured.value.retryable is False


def test_async_task_lease_covers_generation_and_callback_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            llm_timeout_seconds=60,
            agent_task_visibility_timeout_seconds=10,
        ),
    )

    assert worker._async_task_visibility_timeout_seconds() == 95


def test_async_task_lease_preserves_larger_operator_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker,
        "get_settings",
        lambda: SimpleNamespace(
            llm_timeout_seconds=60,
            agent_task_visibility_timeout_seconds=300,
        ),
    )

    assert worker._async_task_visibility_timeout_seconds() == 300
