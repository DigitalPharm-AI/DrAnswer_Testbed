from __future__ import annotations

from typing import Any

_NUTRIENT_ROWS = (
    ("protein", "단백질", "g"),
    ("sodium", "나트륨", "mg"),
    ("fat", "지방", "g"),
    ("carbohydrates", "탄수화물", "g"),
)


def food_candidate_tables(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project food candidates to the v1.3 chat table contract."""

    return [food_candidate_table(candidate) for candidate in candidates]


def food_candidate_table(candidate: dict[str, Any]) -> dict[str, Any]:
    food_name = str(candidate.get("food_name") or "").strip()
    selection_value = str(
        candidate.get("selection_value") or ""
    ).strip()
    nutrients = candidate.get("nutrients")
    if not isinstance(nutrients, dict):
        nutrients = {}

    nutrient_summary = " · ".join(
        f"{label} {_nutrient_value(nutrients.get(key), unit)}"
        for key, label, unit in _NUTRIENT_ROWS
    )
    return {
        "table_title": selection_value or food_name or "음식 정보",
        "rows": [
            {
                "column": "기준 제공량",
                "value": _display_text(candidate.get("portion")),
            },
            {
                "column": "열량",
                "value": _nutrient_value(
                    nutrients.get("calories"),
                    "kcal",
                ),
            },
            {
                "column": "영양성분",
                "value": nutrient_summary,
            },
        ],
    }


def _display_text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text or "확인 불가"


def _nutrient_value(value: Any, unit: str) -> str:
    if value is None:
        return "확인 불가"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "확인 불가"
    return f"{_format_number(number)} {unit}"


def _format_number(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")
