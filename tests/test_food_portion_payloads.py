from __future__ import annotations

from agent_app.integration.food_selection_payloads import (
    food_portion_input_request,
)


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
