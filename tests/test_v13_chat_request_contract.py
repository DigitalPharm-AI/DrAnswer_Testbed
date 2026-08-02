from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from shared.chat_contracts import ChatSyncRequest
from system_app.contracts_v13 import BackendChatRequest
from system_app.ui_contracts import UiChatRequest

REQUEST_ID = "req_0000000000000001"
USER_MESSAGE_ID = "user_msg_0000000000000001"
PATIENT_ID = "patient_0000000000000001"
ASSISTANT_MESSAGE_ID = "assistant_msg_0000000000000001"


def _backend_or_ui_payload(model, **values) -> dict:
    payload = {"message": "안녕하세요.", **values}
    if model is BackendChatRequest:
        payload["patient_id"] = PATIENT_ID
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        {
            "request_id": REQUEST_ID,
            "message_id": USER_MESSAGE_ID,
            "patient_id": PATIENT_ID,
            "message": "안녕하세요.",
            "message_at": datetime.now(UTC),
        },
        {
            "request_id": REQUEST_ID,
            "message_id": USER_MESSAGE_ID,
            "patient_id": PATIENT_ID,
            "requested_return_type": None,
            "message": "안녕하세요.",
            "message_at": datetime.now(UTC),
        },
    ],
)
def test_agent_chat_requires_non_null_requested_return_type(
    payload: dict,
) -> None:
    with pytest.raises(ValidationError):
        ChatSyncRequest.model_validate(payload)


@pytest.mark.parametrize(
    "model",
    [BackendChatRequest, UiChatRequest],
)
@pytest.mark.parametrize(
    "return_type",
    [None, pytest.param("missing", id="missing")],
)
def test_backend_and_ui_chat_require_requested_return_type(
    model,
    return_type,
) -> None:
    payload = _backend_or_ui_payload(model)
    if return_type is None:
        payload["requested_return_type"] = None
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_text_is_the_canonical_general_chat_return_type() -> None:
    agent = ChatSyncRequest(
        request_id=REQUEST_ID,
        message_id=USER_MESSAGE_ID,
        patient_id=PATIENT_ID,
        requested_return_type="text",
        message="안녕하세요.",
        message_at=datetime.now(UTC),
    )
    backend = BackendChatRequest(
        patient_id=PATIENT_ID,
        requested_return_type="text",
        message="안녕하세요.",
    )
    ui = UiChatRequest(
        requested_return_type="text",
        message="안녕하세요.",
    )

    assert agent.requested_return_type == "text"
    assert backend.requested_return_type == "text"
    assert backend.source_message_id is None
    assert ui.requested_return_type == "text"
    assert ui.source_message_id is None


@pytest.mark.parametrize("model", [BackendChatRequest, UiChatRequest])
def test_structured_reply_still_requires_source_message(model) -> None:
    with pytest.raises(
        ValidationError,
        match="source_message_id_required_for_structured_response",
    ):
        model.model_validate(
            _backend_or_ui_payload(
                model,
                message="첫 번째",
                requested_return_type="selection_box",
            )
        )

    request = model.model_validate(
        _backend_or_ui_payload(
            model,
            message="첫 번째",
            requested_return_type="selection_box",
            source_message_id=ASSISTANT_MESSAGE_ID,
        )
    )
    assert request.source_message_id == ASSISTANT_MESSAGE_ID
