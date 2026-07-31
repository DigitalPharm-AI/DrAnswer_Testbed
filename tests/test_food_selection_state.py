from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from agent_app.integration.selection_state import (
    CONSUMED,
    RESOLVED,
    FoodSelectionStateStore,
    SelectionStateError,
)
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import AgentPendingSelection
from shared.schemas import AgentResponse
from shared.settings import get_settings

PATIENT_ID = "patient_0000000000000701"
ORIGIN_MESSAGE_ID = "user_msg_0000000000000701"
SOURCE_REQUEST_ID = "req_0000000000000701"
RESPONSE_MESSAGE_ID = "user_msg_0000000000000702"
MESSAGE_AT = datetime(
    2026,
    4,
    20,
    9,
    30,
    tzinfo=UTC,
)


@pytest.fixture(autouse=True)
def clear_pending_selections() -> None:
    with SessionLocal() as session:
        session.execute(delete(AgentPendingSelection))
        session.commit()


def _food_response() -> AgentResponse:
    candidates = [
        {
            "food_ref_id": "D402-145000000-0001",
            "food_name": "토스트(식빵)",
            "category": "빵 및 과자류",
            "portion": "230.3g",
            "nutrients": {
                "calories": 84.0,
                "carbohydrates": 15.55,
                "protein": 2.5,
                "fat": 1.33,
                "sodium": 140.0,
            },
            "source": "식품의약품안전처",
            "manufacturer": "해당없음",
        },
        {
            "food_ref_id": "toast-garlic",
            "food_name": "토스트_마늘토스트",
            "portion": "165.0g",
            "nutrients": {
                "calories": 306.0,
                "protein": 7.27,
                "sodium": 448.0,
            },
        },
    ]
    return AgentResponse(
        trace_id="trace-food-selection",
        agent_name="multiturn_chat_agent",
        prompt_version_id="test",
        decision_type="tool_call",
        structured_payload={
            "food_candidates": candidates,
            "food_searches": [
                {
                    "query": "토스트",
                    "meal_type": "breakfast",
                    "candidates": candidates,
                }
            ],
        },
        human_summary="토스트 종류를 선택해 주세요.",
    )


def test_food_candidate_snapshot_survives_string_selection_box() -> None:
    store = FoodSelectionStateStore(
        SessionLocal,
        settings=get_settings(),
    )
    prepared = store.prepare_from_agent_response(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        source_chat_request_id=SOURCE_REQUEST_ID,
        trace_id="trace-food-selection",
        message_at=MESSAGE_AT,
        response=_food_response(),
    )
    assert prepared is not None
    assert prepared.candidate_count == 2

    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingSelection))
        assert row is not None
        assert row.status == "PENDING"
        assert row.payload_ciphertext.startswith("v1.")
        assert "토스트" not in row.payload_ciphertext
        assert row.encryption_key_id.endswith(
            ":food-selection-v1"
        )

    resolved = store.resolve(
        patient_id=PATIENT_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="토스트(식빵)",
    )
    assert resolved is not None
    assert resolved.candidate["food_ref_id"] == (
        "D402-145000000-0001"
    )
    assert resolved.candidate["source"] == (
        "식품의약품안전처"
    )
    assert resolved.record_arguments() == {
        "meal_type": "breakfast",
        "meal_date": "2026-04-20",
        "foods": [
            {
                "food_ref_id": "D402-145000000-0001",
                "food_name": "토스트(식빵)",
                "portion": "230.3g",
                "nutrients": {
                    "calories": 84.0,
                    "protein": 2.5,
                    "sodium": 140.0,
                    "fat": 1.33,
                    "carbohydrates": 15.55,
                },
            }
        ],
    }

    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingSelection))
        assert row is not None
        assert row.status == RESOLVED
        assert row.selected_value_hash
        assert row.resolved_by_message_id == RESPONSE_MESSAGE_ID

    store.consume(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
    )
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingSelection))
        assert row is not None
        assert row.status == CONSUMED


def test_food_selection_rejects_value_outside_original_card() -> None:
    store = FoodSelectionStateStore(
        SessionLocal,
        settings=get_settings(),
    )
    store.prepare_from_agent_response(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        source_chat_request_id=SOURCE_REQUEST_ID,
        trace_id="trace-food-selection",
        message_at=MESSAGE_AT,
        response=_food_response(),
    )

    with pytest.raises(
        SelectionStateError,
        match="food_selection_value_invalid",
    ):
        store.resolve(
            patient_id=PATIENT_ID,
            current_user_message_id=RESPONSE_MESSAGE_ID,
            originating_user_message_id=(
                ORIGIN_MESSAGE_ID
            ),
            submitted_value="존재하지 않는 음식",
        )
