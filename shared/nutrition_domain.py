from typing import Final

MEAL_TYPE_VALUES: Final[tuple[str, ...]] = (
    "breakfast",
    "lunch",
    "dinner",
    "snack",
)
MEAL_TYPES: Final[frozenset[str]] = frozenset(MEAL_TYPE_VALUES)
MEAL_TYPE_LABELS: Final[dict[str, str]] = {
    "breakfast": "아침",
    "lunch": "점심",
    "dinner": "저녁",
    "snack": "간식",
}


def meal_type_label(meal_type: str) -> str:
    return MEAL_TYPE_LABELS.get(meal_type, meal_type)


def normalize_meal_type(value: object) -> str:
    meal_type = str(value or "")
    return meal_type if meal_type in MEAL_TYPES else ""
