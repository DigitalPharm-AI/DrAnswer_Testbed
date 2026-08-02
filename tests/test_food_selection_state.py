from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from agent_app.integration.food_selection_payloads import (
    food_portion_input_request,
)
from agent_app.integration.selection_errors import SelectionStateError
from agent_app.integration.selection_state import (
    CONSUMED,
    RESOLVED,
    FoodSelectionStateStore,
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


def test_food_selection_batch_advances_and_combines_foods() -> None:
    store = FoodSelectionStateStore(
        SessionLocal,
        settings=get_settings(),
    )
    first_candidates = [
        {
            "food_ref_id": "food-toast",
            "food_name": "토스트(식빵)",
            "portion": "1장",
            "nutrients": {"calories": 120.0},
        }
    ]
    second_candidates = [
        {
            "food_ref_id": "food-egg",
            "food_name": "달걀_삶은 달걀",
            "portion": "1개",
            "nutrients": {
                "calories": 75.0,
                "protein": 6.0,
            },
        }
    ]
    response = AgentResponse(
        trace_id="trace-food-selection-batch",
        agent_name="multiturn_chat_agent",
        prompt_version_id="test",
        decision_type="tool_call",
        structured_payload={
            "food_candidates": first_candidates,
            "food_searches": [
                {
                    "query": "토스트",
                    "meal_type": "breakfast",
                    "candidates": first_candidates,
                },
                {
                    "query": "삶은 계란",
                    "meal_type": "breakfast",
                    "candidates": second_candidates,
                },
            ],
        },
        human_summary="음식 후보를 선택해 주세요.",
    )
    prepared = store.prepare_from_agent_response(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        source_chat_request_id=SOURCE_REQUEST_ID,
        trace_id="trace-food-selection-batch",
        message_at=MESSAGE_AT,
        response=response,
    )
    assert prepared is not None

    first_transition = store.resolve(
        patient_id=PATIENT_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="토스트(식빵)",
    )
    assert first_transition is not None
    assert first_transition.kind == "next_selection"
    assert first_transition.next_query == "삶은 계란"
    assert first_transition.next_group_number == 2
    assert first_transition.total_groups == 2
    with pytest.raises(
        SelectionStateError,
        match="food_selection_batch_not_completed",
    ):
        first_transition.record_arguments()

    final_message_id = "user_msg_0000000000000703"
    completed = store.resolve(
        patient_id=PATIENT_ID,
        current_user_message_id=final_message_id,
        originating_user_message_id=RESPONSE_MESSAGE_ID,
        submitted_value="달걀_삶은 달걀",
    )
    assert completed is not None
    assert completed.kind == "completed"
    assert completed.record_arguments() == {
        "meal_type": "breakfast",
        "meal_date": "2026-04-20",
        "foods": [
            {
                "food_ref_id": "food-toast",
                "food_name": "토스트(식빵)",
                "portion": "1장",
                "nutrients": {
                    "calories": 120.0,
                },
            },
            {
                "food_ref_id": "food-egg",
                "food_name": "달걀_삶은 달걀",
                "portion": "1개",
                "nutrients": {
                    "calories": 75.0,
                    "protein": 6.0,
                },
            },
        ],
    }

    store.consume(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        current_user_message_id=final_message_id,
    )


def test_food_portion_input_updates_portions_and_scales_nutrients() -> None:
    store = FoodSelectionStateStore(
        SessionLocal,
        settings=get_settings(),
    )
    prepared = store.prepare_from_agent_response(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        source_chat_request_id=SOURCE_REQUEST_ID,
        trace_id="trace-food-portion",
        message_at=MESSAGE_AT,
        response=_food_response(),
    )
    assert prepared is not None

    selected = store.resolve(
        patient_id=PATIENT_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="토스트(식빵)",
    )
    assert selected is not None
    assert selected.portions_confirmed is False
    input_request = food_portion_input_request(
        selected.selected_candidates
    )
    assert input_request["message_title"] == "섭취량 입력"
    assert input_request["inputs"] == [
        {
            "type": "number",
            "label": "1. 토스트(식빵) 섭취량",
            "value": 230.3,
            "options": {
                "unit": "g",
                "lower": 1.0,
                "upper": 5000.0,
                "selections": None,
            },
        }
    ]

    portion_message_id = "user_msg_0000000000000704"
    completed = store.resolve_portions(
        patient_id=PATIENT_ID,
        current_user_message_id=portion_message_id,
        originating_user_message_id=RESPONSE_MESSAGE_ID,
        submitted_values={
            "1. 토스트(식빵) 섭취량": 115.15,
        },
    )
    assert completed is not None
    assert completed.portions_confirmed is True
    assert completed.record_arguments() == {
        "meal_type": "breakfast",
        "meal_date": "2026-04-20",
        "foods": [
            {
                "food_ref_id": "D402-145000000-0001",
                "food_name": "토스트(식빵)",
                "portion": "115.15g",
                "nutrients": {
                    "calories": 42.0,
                    "protein": 1.25,
                    "sodium": 70.0,
                    "fat": 0.665,
                    "carbohydrates": 7.775,
                },
            }
        ],
    }

    store.consume(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        current_user_message_id=portion_message_id,
    )
