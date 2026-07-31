from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from agent_app.integration.feedback_contracts import (
    ChatFeedbackAccepted,
    ChatFeedbackRequest,
)
from shared.async_v13_contracts import (
    AsyncEventAccepted,
    AsyncResultCallbackAck,
    MissedDoseEventRequest,
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposalAck,
)
from shared.backend_read_contract import BACKEND_READ_VIEW_DEFINITIONS
from shared.backend_v13_contracts import (
    CommonErrorResponse,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    RecordChangeRequest,
    RecordChangeResponse,
)
from shared.chat_contracts import (
    ChatErrorResponse,
    ChatStreamEvent,
    ChatSyncRequest,
    ChatSyncResponse,
    agent_chat_payload,
)
from shared.contract_boundary import is_retired_conversation_key
from shared.schemas import ToolCallResult
from system_app.contracts_ui_feedback import (
    UiChatOpinionAcceptedResponse,
    UiChatOpinionRequest,
    UiFeedbackErrorResponse,
)
from system_app.contracts_v13 import BackendChatRequest, BackendChatResponse
from system_app.ui_contracts import (
    UiChatHistoryResponse,
    UiChatRequest,
    UiChatSyncResponse,
    UiDashboardResponse,
    UiErrorResponse,
)


PUBLIC_BOUNDARY_MODELS = (
    ChatSyncRequest,
    ChatSyncResponse,
    ChatStreamEvent,
    ChatErrorResponse,
    ChatFeedbackRequest,
    ChatFeedbackAccepted,
    MissedDoseEventRequest,
    AsyncEventAccepted,
    MissedDoseResultCallback,
    AsyncResultCallbackAck,
    NotificationPolicyChangeProposalRequest,
    NotificationPolicyProposalAck,
    RecordChangeRequest,
    RecordChangeResponse,
    NotificationPolicyChangeRequest,
    NotificationPolicyChangeResponse,
    CommonErrorResponse,
    BackendChatRequest,
    BackendChatResponse,
    UiChatRequest,
    UiChatSyncResponse,
    UiChatHistoryResponse,
    UiDashboardResponse,
    UiErrorResponse,
    UiChatOpinionRequest,
    UiChatOpinionAcceptedResponse,
    UiFeedbackErrorResponse,
)


def _retired_key_paths(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> list[tuple[str, ...]]:
    paths: list[tuple[str, ...]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = (*path, str(key))
            if is_retired_conversation_key(key):
                paths.append(next_path)
            paths.extend(_retired_key_paths(item, path=next_path))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            paths.extend(
                _retired_key_paths(
                    item,
                    path=(*path, str(index)),
                )
            )
    return paths


def test_active_boundary_model_schemas_do_not_publish_conversation_id() -> None:
    violations = {
        model.__name__: _retired_key_paths(model.model_json_schema())
        for model in PUBLIC_BOUNDARY_MODELS
    }

    assert {
        name: paths
        for name, paths in violations.items()
        if paths
    } == {}


@pytest.mark.parametrize(
    "model,payload",
    (
        (
            ChatSyncRequest,
            {
                "request_id": "req_000000001234abcd",
                "message_id": "user_msg_000000001234abcd",
                "patient_id": "patient_000000001234abcd",
                "requested_return_type": "text",
                "message": "안녕하세요",
                "message_at": datetime.now(UTC),
                "conversation_id": "retired",
            },
        ),
        (
            BackendChatRequest,
            {
                "patient_id": "patient_000000001234abcd",
                "message": "안녕하세요",
                "conversation_id": "retired",
            },
        ),
        (
            UiChatRequest,
            {
                "message": "안녕하세요",
                "conversation_id": "retired",
            },
        ),
        (
            ChatFeedbackRequest,
            {
                "request_id": "req_000000001234abcd",
                "message_id": "assistant_msg_000000001234abcd",
                "patient_id": "patient_000000001234abcd",
                "reaction": "like",
                "feedback_text": None,
                "feedback_at": datetime.now(UTC),
                "conversation_id": "retired",
            },
        ),
    ),
)
def test_strict_boundary_requests_reject_top_level_conversation_id(
    model,
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        model.model_validate(payload)


def test_backend_context_removes_nested_retired_identifiers_before_agent() -> None:
    request = ChatSyncRequest(
        request_id="req_000000001234abcd",
        message_id="user_msg_000000001234abcd",
        patient_id="patient_000000001234abcd",
        requested_return_type="text",
        message="오늘 복약 상태를 알려줘",
        message_at=datetime.now(UTC),
    )

    payload = agent_chat_payload(
        request,
        backend_context={
            "conversation_id": "top-level",
            "recent_chat": [
                {
                    "message_id": "assistant_msg_00000000deadbeef",
                    "message": {
                        "conversationId": "nested",
                        "text": "안전한 대화",
                    },
                }
            ],
            "approved_mutation_confirmation": {
                "nested": {
                    "Conversation-ID": "deep",
                    "keep": True,
                }
            },
        },
    )

    assert _retired_key_paths(payload) == []
    assert payload["patient_id"] == request.patient_id
    assert (
        payload["context"]["recent_chat"][0]["message"]["text"]
        == "안전한 대화"
    )
    assert "approved_mutation_confirmation" not in payload["context"]


def test_generic_tool_and_error_payloads_remove_nested_retired_identifiers() -> None:
    result = ToolCallResult(
        tool_name="example",
        status="success",
        response={
            "ConversationId": "retired",
            "result": {"ok": True},
        },
    )

    assert result.response == {"result": {"ok": True}}


def test_active_chat_read_view_does_not_project_internal_conversation_column() -> None:
    definition = BACKEND_READ_VIEW_DEFINITIONS["ai_v13_chat_messages"]
    select_sql = str(definition["select"])

    assert "conversation_id" not in definition["columns"]
    assert "message.conversation_id" not in select_sql
    assert "- 'conversation_id'" in select_sql
    assert "- 'conversationId'" in select_sql
