from __future__ import annotations

import json
import logging

import pytest

from agent_app.agents.multiturn_approved_write import (
    execute_approved_supervisor_write,
)
from agent_app.errors import AgentExecutionError, public_processing_error
from agent_app.tools.backend_write import _tool_result_from_response_body
from shared.schemas import ToolCallResult
from system_app.routes.backend_v13 import _processing_error_response


def test_backend_programming_error_is_logged_and_not_retryable(caplog) -> None:
    try:
        raise TypeError("sensitive clinical text must not enter the log")
    except TypeError as exc:
        with caplog.at_level(logging.ERROR, logger="uvicorn.error"):
            response = _processing_error_response(
                request_id="req_0000000000000001",
                api_path="/agent/sync/record-change",
                resource_type="medication_dose_event",
                operation="update",
                exc=exc,
            )

    body = json.loads(response.body)
    assert response.status_code == 500
    assert body["error"]["code"] == "BACKEND_PROCESSING_ERROR"
    assert body["error"]["retryable"] is False
    assert "backend_v13_processing_failed" in caplog.text
    assert "TypeError" in caplog.text
    assert "test_backend_v13_processing_failure.py" in caplog.text
    assert "sensitive clinical text" not in caplog.text


def test_backend_transient_error_remains_retryable() -> None:
    response = _processing_error_response(
        request_id="req_0000000000000002",
        api_path="/agent/sync/record-change",
        resource_type="medication_dose_event",
        operation="update",
        exc=TimeoutError("temporary timeout"),
    )

    body = json.loads(response.body)
    assert body["error"]["retryable"] is True


@pytest.mark.parametrize("retryable", [False, True])
def test_backend_tool_result_preserves_retryability(retryable: bool) -> None:
    result = _tool_result_from_response_body(
        "update_medication_dose_event_status",
        "req_0000000000000003",
        {
            "request_id": "req_0000000000000003",
            "error": {
                "code": "BACKEND_PROCESSING_ERROR",
                "message": "Backend processing failed.",
                "retryable": retryable,
                "details": None,
            },
        },
    )

    assert result.status == "error"
    assert result.retryable is retryable


@pytest.mark.asyncio
async def test_approved_write_propagates_non_retryable_tool_failure() -> None:
    class FailedToolRuntime:
        async def execute(self, *args, **kwargs):
            return (
                [{"name": "update_medication_dose_event_status"}],
                [
                    ToolCallResult(
                        tool_name="update_medication_dose_event_status",
                        status="error",
                        error="BACKEND_PROCESSING_ERROR",
                        retryable=False,
                    )
                ],
            )

    with pytest.raises(AgentExecutionError) as exc_info:
        await execute_approved_supervisor_write(
            provider=object(),
            tool_runtime=FailedToolRuntime(),
            trace_id="trace_0000000000000001",
            request_payload={},
            tool_call={"name": "update_medication_dose_event_status"},
        )

    public_error = public_processing_error(exc_info.value)
    assert exc_info.value.error_type == "mutation_tool_not_applied"
    assert public_error.code == "TOOL_EXECUTION_FAILED"
    assert public_error.retryable is False
