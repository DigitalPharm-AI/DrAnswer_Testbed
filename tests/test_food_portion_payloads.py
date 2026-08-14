from __future__ import annotations

from agent_app.integration.food_selection_payloads import (
    food_portion_input_request,
)
from agent_app.tools.approval_display import project_record_arguments_to_action_schema
from agent_app.tools.backend_food_queries import food_reference_view


def test_food_portion_input_uses_each_reference_quantity_as_default() -> None:
    input_request = food_portion_input_request(
        [
            {
                "food_name": "토스트_마늘토스트",
                "portion": "500g",
            },
            {
                "food_name": "물_생수",
                "portion": "1000mL",
            },
        ]
    )

    assert input_request["inputs"] == [
        {
            "type": "number",
            "label": "1. 토스트_마늘토스트 섭취량",
            "value": 500.0,
            "options": {
                "unit": "g",
                "lower": 1.0,
                "upper": 5000.0,
                "selections": None,
            },
        },
        {
            "type": "number",
            "label": "2. 물_생수 섭취량",
            "value": 1000.0,
            "options": {
                "unit": "mL",
                "lower": 1.0,
                "upper": 5000.0,
                "selections": None,
            },
        },
    ]


def test_food_reference_view_scales_100g_values_to_serving_size() -> None:
    candidate = food_reference_view(
        {
            "food_ref_id": "gomtang-600",
            "food_name": "곰탕",
            "category": "국",
            "serving_size": 600.0,
            "energy": 24.0,
            "carbohydrate": 0.11,
            "protein": 3.43,
            "fat": 1.12,
            "sodium": 124.0,
            "source": "식품의약품안전처",
            "manufacturer": "",
        }
    )

    assert candidate["portion"] == "600g"
    assert candidate["nutrients"] == {
        "calories": 144.0,
        "carbohydrates": 0.66,
        "protein": 20.58,
        "fat": 6.72,
        "sodium": 744.0,
    }


def test_meal_approval_schema_preserves_food_reference_id() -> None:
    projected = project_record_arguments_to_action_schema(
        "create_nutrition_meal_record",
        {
            "meal_type": "breakfast",
            "foods": [
                {
                    "food_ref_id": "watermelon-punch-200",
                    "food_name": "수박화채",
                    "portion": "200g",
                    "nutrients": {"calories": 84.0},
                }
            ],
        },
    )

    assert projected["foods"][0]["food_ref_id"] == ("watermelon-punch-200")
