from __future__ import annotations

from datetime import UTC, datetime

from shared.chat_contracts import (
    ChatSyncRequest,
    chat_sync_response,
)
from shared.food_selection_candidates import (
    normalize_food_selection_candidates,
)
from shared.schemas import AgentResponse


def _makguksu_candidates() -> list[dict[str, object]]:
    first = {
        "food_ref_id": "makguksu-732-a",
        "food_name": "막국수",
        "category": "면류",
        "portion": "732.6g",
        "nutrients": {
            "calories": 540.0,
            "carbohydrates": 100.0,
            "protein": 18.0,
            "fat": 8.0,
            "sodium": 1200.0,
        },
        "source": "식품의약품안전처",
        "manufacturer": "",
    }
    duplicate = dict(first)
    duplicate["food_ref_id"] = "makguksu-732-b"
    second = {
        **first,
        "food_ref_id": "makguksu-550",
        "portion": "550g",
        "nutrients": {
            "calories": 410.0,
            "carbohydrates": 100.0,
            "protein": 18.0,
            "fat": 8.0,
            "sodium": 1200.0,
        },
    }
    return [first, duplicate, second]


def test_normalize_food_candidates_keeps_distinct_same_name_candidates() -> None:
    candidates = normalize_food_selection_candidates(
        _makguksu_candidates()
    )

    assert [candidate["food_ref_id"] for candidate in candidates] == [
        "makguksu-732-a",
        "makguksu-550",
    ]
    assert [candidate["selection_value"] for candidate in candidates] == [
        "1. 막국수 (732.6g)",
        "2. 막국수 (550g)",
    ]


def test_chat_response_aligns_same_name_buttons_and_tables() -> None:
    response = AgentResponse(
        trace_id="trace-food-candidate-labels",
        agent_name="nutrition_agent",
        prompt_version_id="test",
        decision_type="tool_call",
        structured_payload={
            "food_candidates": _makguksu_candidates(),
        },
        human_summary="막국수 후보를 선택해 주세요.",
    )
    request = ChatSyncRequest(
        request_id="req_0000000000000801",
        message_id="user_msg_0000000000000801",
        patient_id="patient_0000000000000801",
        requested_return_type="text",
        message="점심으로 막국수 먹었어",
        message_at=datetime(2026, 8, 18, 12, tzinfo=UTC),
    )

    result = chat_sync_response(request, response)

    expected = [
        "1. 막국수 (732.6g)",
        "2. 막국수 (550g)",
    ]
    assert result.message.selections == expected
    assert result.message.tables is not None
    assert [table.table_title for table in result.message.tables] == expected
