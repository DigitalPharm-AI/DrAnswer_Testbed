from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from system_app.contracts_ui_feedback import (
    UiChatOpinionAccepted,
    UiChatOpinionRequest,
)
from system_app.ui_contracts import (
    AdvanceClockRequest,
    ApplyMedicationScenarioRequest,
    ResetTestbedRequest,
    UiChatHistoryMessage,
    UiChatRequest,
    UiChatSyncData,
    UiMedicationDose,
    UiNotification,
)


NOW = datetime(2026, 7, 28, 9, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ApplyMedicationScenarioRequest,
            {
                "request_id": "req_000000001234abcd",
                "scenario_id": "scenario-1",
                "schedule_date": "2026-07-28",
            },
        ),
        (
            AdvanceClockRequest,
            {"request_id": "req_000000001234abcd", "minutes": 30},
        ),
        (
            ResetTestbedRequest,
            {"request_id": "req_000000001234abcd", "confirm": True},
        ),
    ],
)
def test_ui_mutation_requests_require_req_8(model, payload) -> None:
    assert model.model_validate(payload).request_id == "req_000000001234abcd"

    for invalid in (
        "1234abcd-0000-0000-0000-000000000000",
        "request_1234abcd",
        "req_1234ABCd",
        "req_1234abc",
        " req_000000001234abcd",
    ):
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "request_id": invalid})


def test_ui_chat_request_validates_optional_request_and_source_ids() -> None:
    request = UiChatRequest.model_validate(
        {
            "message": "첫 번째 후보를 선택할게요.",
            "requested_return_type": "selection_box",
            "request_id": "req_00000000a1b2c3d4",
            "source_message_id": "assistant_msg_000000001020abcd",
        }
    )

    assert request.request_id == "req_00000000a1b2c3d4"
    assert request.source_message_id == "assistant_msg_000000001020abcd"

    with pytest.raises(ValidationError):
        UiChatRequest.model_validate(
            {
                "message": "첫 번째 후보",
                "requested_return_type": "selection_box",
                "request_id": "req_00000000a1b2c3d4",
                "source_message_id": "user_msg_000000001020abcd",
            }
        )


def test_ui_chat_response_and_history_keep_role_specific_message_ids() -> None:
    sync = UiChatSyncData.model_validate(
        {
            "request_id": "req_0000000089abcdef",
            "user_message_id": "user_msg_000000000123abcd",
            "user_sort_sequence": 10,
            "assistant_message_id": "assistant_msg_000000004567abcd",
            "assistant_sort_sequence": 11,
            "message_type": "text",
            "message": {
                "message_title": None,
                "text": "확인했습니다.",
                "tables": None,
                "selections": None,
                "inputs": None,
            },
            "message_at": NOW,
            "display_message_at": NOW,
        }
    )
    assert sync.user_message_id == "user_msg_000000000123abcd"
    assert sync.assistant_message_id == "assistant_msg_000000004567abcd"

    assistant = UiChatHistoryMessage.model_validate(
        {
            "message_id": "assistant_msg_000000004567abcd",
            "sort_sequence": 11,
            "role": "assistant",
            "message_type": "selection_box",
            "message": "선택해 주세요.",
            "content": None,
            "created_at": NOW,
            "processing_status": "completed",
            "response_message_id": "user_msg_000000000123abcd",
            "source_message_id": None,
            "reaction": None,
            "opinion_submitted": False,
            "opinion_submitted_at": None,
        }
    )
    assert assistant.response_message_id == "user_msg_000000000123abcd"
    assert assistant.sort_sequence == 11

    with pytest.raises(ValidationError):
        UiChatHistoryMessage.model_validate(
            {
                **assistant.model_dump(),
                "response_message_id": "assistant_msg_000000004567abcd",
            }
        )


def test_ui_dose_and_notification_links_require_dose_8() -> None:
    dose = UiMedicationDose.model_validate(
        {
            "dose_event_id": "dose_000000001020abcd",
            "medication_name": "암로디핀",
            "treatment_area": "고혈압약",
            "slot_label": "아침",
            "scheduled_for": NOW,
            "status": "scheduled",
            "taken_at": None,
        }
    )
    assert dose.dose_event_id == "dose_000000001020abcd"

    notification = UiNotification.model_validate(
        {
            "id": "notif_0123456789abcdef0123456789abcdef",
            "notification_type": "conversation_alert",
            "title": "미복용 안내",
            "body": "복약 여부를 확인해 주세요.",
            "visible_at": NOW,
            "visible_at_label": "07-28 09:30",
            "acknowledged": False,
            "metadata": {"severity": "reminder"},
            "interaction": {
                "kind": "open_chat",
                "state": "ready",
                "message_id": "assistant_msg_000000004567abcd",
            },
            "dose_status": "missed",
            "related_dose_event_id": "dose_000000001020abcd",
        }
    )
    assert notification.related_dose_event_id == "dose_000000001020abcd"

    with pytest.raises(ValidationError):
        UiMedicationDose.model_validate(
            {**dose.model_dump(), "dose_event_id": "dose_1020abcdef"}
        )


def test_ui_feedback_uses_req_8_and_assistant_msg_8() -> None:
    request = UiChatOpinionRequest.model_validate(
        {
            "request_id": "req_00000000deadbeef",
            "assistant_message_id": "assistant_msg_000000001234abcd",
            "reaction": "like",
            "feedback_at": NOW,
        }
    )
    accepted = UiChatOpinionAccepted(
        status="accepted",
        request_id=request.request_id,
        assistant_message_id=request.assistant_message_id,
        reaction="like",
        opinion_submitted=False,
        opinion_submitted_at=None,
    )
    assert accepted.request_id == "req_00000000deadbeef"

    with pytest.raises(ValidationError):
        UiChatOpinionRequest.model_validate(
            {
                **request.model_dump(),
                "assistant_message_id": "user_msg_000000001234abcd",
            }
        )
