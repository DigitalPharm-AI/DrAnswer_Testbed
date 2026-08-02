from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from agent_app.integration.selection_errors import SelectionStateError
from shared.nutrition_domain import MEAL_TYPES
from shared.schemas import AgentResponse

NUTRITION_FOOD = "nutrition_food"


def build_food_selection_snapshot(
    response: AgentResponse,
    *,
    message_at: datetime,
) -> dict[str, Any] | None:
    structured = response.structured_payload
    if isinstance(
        structured.get("mutation_confirmation"),
        dict,
    ):
        return None

    groups: list[dict[str, Any]] = []
    meal_type = ""
    searches = structured.get("food_searches")
    if isinstance(searches, list):
        for search in searches:
            if not isinstance(search, dict):
                continue
            candidates = _normalized_candidates(search.get("candidates"))
            if not candidates:
                # A batch must not silently drop an unmatched food.
                # Let the LLM ask a clarification instead of opening
                # an incomplete selection workflow.
                return None
            group_meal_type = str(search.get("meal_type") or "").strip()
            if group_meal_type in MEAL_TYPES:
                if meal_type and meal_type != group_meal_type:
                    raise SelectionStateError("food_selection_meal_type_conflict")
                meal_type = group_meal_type
            groups.append(
                {
                    "query": str(search.get("query") or "").strip(),
                    "candidates": candidates,
                    "selected_value": "",
                    "selected_candidate": None,
                    "resolved_by_message_id": "",
                }
            )
    if not groups:
        candidates = _normalized_candidates(structured.get("food_candidates"))
        if candidates:
            groups.append(
                {
                    "query": "",
                    "candidates": candidates,
                    "selected_value": "",
                    "selected_candidate": None,
                    "resolved_by_message_id": "",
                }
            )
    if not groups:
        return None
    if meal_type not in MEAL_TYPES:
        meal_type = ""
    return {
        "selection_type": NUTRITION_FOOD,
        "meal_type": meal_type,
        "meal_date": message_at.date().isoformat(),
        "current_index": 0,
        "groups": groups,
    }


def food_state_candidates(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    groups = food_state_groups(state)
    if not groups:
        raise SelectionStateError("food_selection_candidates_missing")
    return food_group_candidates(groups[0])


def food_state_groups(
    state: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = state.get("groups")
    if not isinstance(raw, list):
        raise SelectionStateError("food_selection_groups_missing")
    return [dict(group) for group in raw if isinstance(group, dict)]


def food_group_candidates(
    group: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = group.get("candidates")
    if not isinstance(raw, list):
        raise SelectionStateError("food_selection_candidates_missing")
    return [dict(candidate) for candidate in raw if isinstance(candidate, dict)]


def _normalized_candidates(
    raw_candidates: Any,
) -> list[dict[str, Any]]:
    if not isinstance(raw_candidates, list):
        return []
    candidates: list[dict[str, Any]] = []
    seen_labels: set[str] = set()
    for raw in raw_candidates:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("food_name") or "").strip()
        if not label or label in seen_labels:
            continue
        candidate = dict(raw)
        candidate["food_name"] = label
        candidate["selection_value"] = label
        candidates.append(candidate)
        seen_labels.add(label)
    return candidates


def selected_food_candidates(
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for group in groups:
        candidate = group.get("selected_candidate")
        if isinstance(candidate, dict):
            selected.append(dict(candidate))
    return selected


def public_food_candidate(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    public = dict(candidate)
    public.pop("selection_value", None)
    return public


_PORTION_PATTERN = re.compile(
    r"^\s*(?P<quantity>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>g|mg|ml|mL|L|인분|개)\s*$",
    re.IGNORECASE,
)


def food_portion_input_request(
    selected_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    definitions = food_portion_definitions(selected_candidates)
    if not definitions:
        raise SelectionStateError("food_portion_input_candidates_missing")
    return {
        "message_title": "섭취량 입력",
        "text": ("선택한 음식별 실제 섭취량을 입력해 주세요. 입력한 양을 기준으로 영양소를 계산한 뒤 기록 내용을 확인합니다."),
        "tables": None,
        "selections": None,
        "inputs": [
            {
                "type": "number",
                "label": definition["label"],
                "value": definition["reference_quantity"],
                "options": {
                    "unit": definition["unit"],
                    "lower": definition["lower"],
                    "upper": definition["upper"],
                    "selections": None,
                },
            }
            for definition in definitions
        ],
    }


def food_portion_definitions(
    selected_candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for index, candidate in enumerate(
        selected_candidates,
        start=1,
    ):
        food_name = str(candidate.get("food_name") or "").strip()
        if not food_name:
            raise SelectionStateError("food_portion_input_food_name_missing")
        reference_quantity, unit = _reference_portion(candidate.get("portion"))
        if unit in {"g", "mL"}:
            lower, upper = 1.0, 5000.0
        elif unit == "mg":
            lower, upper = 1.0, 500000.0
        elif unit == "L":
            lower, upper = 0.01, 20.0
        else:
            lower, upper = 0.1, 20.0
        definitions.append(
            {
                "label": f"{index}. {food_name} 섭취량",
                "unit": unit,
                "lower": lower,
                "upper": upper,
                "reference_quantity": reference_quantity,
            }
        )
    return definitions


def _reference_portion(value: Any) -> tuple[float, str]:
    text = str(value or "").strip()
    match = _PORTION_PATTERN.fullmatch(text)
    if match is None:
        return 1.0, "인분"
    quantity = float(match.group("quantity"))
    unit = match.group("unit")
    normalized_unit = {
        "ml": "mL",
        "l": "L",
    }.get(unit.lower(), unit.lower() if unit.lower() in {"g", "mg"} else unit)
    return quantity, normalized_unit


def format_food_quantity(value: float) -> str:
    if value.is_integer():
        return str(int(value))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def scale_food_nutrients(
    raw: Any,
    factor: float,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    scaled: dict[str, Any] = {}
    for key, value in raw.items():
        if value is None:
            scaled[key] = None
            continue
        try:
            scaled[key] = round(float(value) * factor, 6)
        except (TypeError, ValueError):
            scaled[key] = value
    return scaled
