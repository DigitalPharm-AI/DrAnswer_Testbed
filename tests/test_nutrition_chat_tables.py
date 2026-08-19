from __future__ import annotations

from shared.nutrition_chat_tables import (
    food_candidate_table,
    food_candidate_tables,
)


def test_food_candidate_table_uses_reference_portion_nutrients() -> None:
    table = food_candidate_table(
        {
            "food_name": "구운 닭가슴살",
            "portion": "100g",
            "nutrients": {
                "calories": 165,
                "protein": 31,
                "sodium": 74,
                "fat": 3.6,
                "carbohydrates": 0,
            },
        }
    )

    assert table == {
        "table_title": "구운 닭가슴살",
        "rows": [
            {"column": "기준 제공량", "value": "100g"},
            {"column": "열량", "value": "165 kcal"},
            {
                "column": "영양성분",
                "value": (
                    "단백질 31 g · 나트륨 74 mg · "
                    "지방 3.6 g · 탄수화물 0 g"
                ),
            },
        ],
    }


def test_food_candidate_tables_preserve_candidate_order_and_missing_values() -> None:
    tables = food_candidate_tables(
        [
            {
                "food_name": "첫 번째 음식",
                "portion": "200g",
                "nutrients": {"calories": 10, "protein": 0},
            },
            {
                "food_name": "두 번째 음식",
                "portion": "",
                "nutrients": {},
            },
        ]
    )

    assert [table["table_title"] for table in tables] == [
        "첫 번째 음식",
        "두 번째 음식",
    ]
    assert tables[0]["rows"][2]["value"].startswith("단백질 0 g")
    assert tables[1]["rows"][0]["value"] == "확인 불가"
    assert tables[1]["rows"][1]["value"] == "확인 불가"


def test_food_candidate_table_prefers_selection_value_for_title() -> None:
    table = food_candidate_table(
        {
            "food_name": "막국수",
            "selection_value": "2. 막국수 (550g)",
            "portion": "550g",
            "nutrients": {},
        }
    )

    assert table["table_title"] == "2. 막국수 (550g)"
