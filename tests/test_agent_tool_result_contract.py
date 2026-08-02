from __future__ import annotations

import pytest

from agent_app.tools.results import tool_calls_payload, tool_result_summary
from shared.schemas import ToolCallResult
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    DELETE_NUTRITION_FOOD_RECORD,
    DELETE_NUTRITION_MEAL_RECORD,
    GET_MEDICATION_DOSE_STATUS,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_NUTRITION_DAILY_SUMMARY,
    GET_NUTRITION_MEAL_RECORD_LIST,
    GET_NUTRITION_PREFERENCE_SUMMARY,
    GET_NUTRITION_RECOMMENDATION_CANDIDATES,
    GET_PRO_CTCAE_QUESTIONNAIRE,
    GET_SIDE_EFFECT_HISTORY,
    PROPOSE_NOTIFICATION_POLICY,
    SEARCH_NUTRITION_FOOD_CANDIDATES,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
    UPSERT_NUTRITION_PREFERENCE_FACT,
)


@pytest.mark.parametrize(
    ("tool_name", "payload_key"),
    [
        (GET_MEDICATION_DOSE_STATUS, "medication_dose_status"),
        (UPDATE_MEDICATION_DOSE_EVENT_STATUS, "dose_taken_result"),
        (CREATE_NUTRITION_MEAL_RECORD, "nutrition_meal_result"),
        (UPDATE_NUTRITION_MEAL_RECORD, "nutrition_meal_update_result"),
        (DELETE_NUTRITION_MEAL_RECORD, "nutrition_meal_delete_result"),
        (UPDATE_NUTRITION_FOOD_RECORD, "nutrition_food_update_result"),
        (DELETE_NUTRITION_FOOD_RECORD, "nutrition_food_delete_result"),
        (UPSERT_NUTRITION_PREFERENCE_FACT, "nutrition_preference_result"),
        (PROPOSE_NOTIFICATION_POLICY, "policy_apply_result"),
    ],
)
def test_success_tool_results_keep_their_public_projection(
    tool_name: str,
    payload_key: str,
) -> None:
    response = {"receipt": f"{tool_name}-receipt"}

    payload = tool_calls_payload(
        [{"name": tool_name, "arguments": {}}],
        [
            ToolCallResult(
                tool_name=tool_name,
                status="success",
                response=response,
            )
        ],
    )

    assert payload[payload_key] == response


def test_error_result_does_not_create_a_success_projection() -> None:
    payload = tool_calls_payload(
        [],
        [
            ToolCallResult(
                tool_name=CREATE_NUTRITION_MEAL_RECORD,
                status="error",
                error="write_failed",
            )
        ],
    )

    assert "nutrition_meal_result" not in payload


def test_multiple_pro_ctcae_results_are_preserved_in_execution_order() -> None:
    nausea = {"input_symptom": "메스꺼움", "questions": []}
    vomiting = {"input_symptom": "구토", "questions": []}
    payload = tool_calls_payload(
        [
            {
                "name": GET_PRO_CTCAE_QUESTIONNAIRE,
                "arguments": {"symptom_text": "메스꺼움"},
            },
            {
                "name": GET_PRO_CTCAE_QUESTIONNAIRE,
                "arguments": {"symptom_text": "구토"},
            },
        ],
        [
            ToolCallResult(
                tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
                status="success",
                response=nausea,
            ),
            ToolCallResult(
                tool_name=GET_PRO_CTCAE_QUESTIONNAIRE,
                status="success",
                response=vomiting,
            ),
        ],
    )

    assert payload["ae_pro_ctcae_items"] == [nausea, vomiting]
    assert payload["ae_pro_ctcae"] == nausea


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            ToolCallResult(
                tool_name=GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                status="success",
                response={"suspected": True},
            ),
            "현재 복약정보와 의약품 부작용 기준정보에서 관련 가능성이 확인되었습니다.",
        ),
        (
            ToolCallResult(
                tool_name=GET_SIDE_EFFECT_HISTORY,
                status="success",
                response={
                    "records": [
                        {"suspected": True},
                        {"suspected": False},
                    ]
                },
            ),
            "부작용 이력 2건을 확인했습니다. 의심 기록은 1건입니다.",
        ),
        (
            ToolCallResult(
                tool_name=GET_MEDICATION_DOSE_STATUS,
                status="success",
                response={
                    "target_date": "2026-04-20",
                    "dose_events": [
                        {"status": "taken"},
                        {"status": "missed"},
                        {"status": "scheduled"},
                    ],
                },
            ),
            "2026-04-20 복약 일정은 총 3건이고, 완료 1건, 미복용 1건, 예정 1건입니다.",
        ),
        (
            ToolCallResult(
                tool_name=CREATE_NUTRITION_MEAL_RECORD,
                status="success",
                response={
                    "meal": {"meal_label": "아침"},
                    "daily_summary": {
                        "summary": {"exceeded_nutrients": ["나트륨"]}
                    },
                },
            ),
            "아침 식사를 기록했습니다. 오늘 기준 초과 항목은 나트륨입니다.",
        ),
        (
            ToolCallResult(
                tool_name=GET_NUTRITION_DAILY_SUMMARY,
                status="success",
                response={
                    "daily_summary": {
                        "total_meals": 2,
                        "summary": {"exceeded_nutrients": []},
                    }
                },
            ),
            "오늘 식사 2건 기준으로 현재 초과 항목은 없습니다.",
        ),
        (
            ToolCallResult(
                tool_name=GET_NUTRITION_MEAL_RECORD_LIST,
                status="success",
                response={"total": 3},
            ),
            "해당 날짜에 기록된 식사는 3건입니다.",
        ),
        (
            ToolCallResult(
                tool_name=SEARCH_NUTRITION_FOOD_CANDIDATES,
                status="success",
                response={
                    "candidates": [
                        {"food_name": "토스트"},
                        {"food_name": "우유"},
                    ]
                },
            ),
            "음식 후보를 찾았습니다: 토스트, 우유",
        ),
        (
            ToolCallResult(
                tool_name=UPSERT_NUTRITION_PREFERENCE_FACT,
                status="success",
                response={
                    "fact": {
                        "object_label": "땅콩",
                        "predicate_label": "알레르기",
                    }
                },
            ),
            "땅콩에 대한 알레르기 정보를 저장했습니다.",
        ),
        (
            ToolCallResult(
                tool_name=GET_NUTRITION_RECOMMENDATION_CANDIDATES,
                status="success",
                response={
                    "recommendations": [{"food_name": "비빔밥"}],
                    "blocked_count": 2,
                },
            ),
            "조건에 맞는 음식 1가지를 찾았습니다: 비빔밥 (선호도 제한으로 2가지 제외됨)",
        ),
        (
            ToolCallResult(
                tool_name=GET_NUTRITION_PREFERENCE_SUMMARY,
                status="success",
                response={
                    "preferences": {"counts": {"hard": 1, "soft": 2}}
                },
            ),
            "저장된 영양 선호도는 제한 1건, 선호/비선호 2건입니다.",
        ),
        (
            ToolCallResult(
                tool_name=PROPOSE_NOTIFICATION_POLICY,
                status="skipped",
                response={"reason": "policy_confirmation_required"},
            ),
            "알림 정책 변경 후보를 만들었습니다. 확인 후 반영됩니다.",
        ),
    ],
)
def test_tool_result_summary_keeps_tool_specific_patient_text(
    result: ToolCallResult,
    expected: str,
) -> None:
    assert tool_result_summary([result], "기본 문장") == expected
