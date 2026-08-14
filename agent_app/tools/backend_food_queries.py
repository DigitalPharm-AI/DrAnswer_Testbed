from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from sqlalchemy import text


def normalize_food_search_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def expanded_food_candidate_rows(
    connection,
    *,
    normalized_query: str,
    limit: int,
    max_rows: int,
) -> list[Any]:
    """Return deterministic spelling and compound-name food matches."""

    anchors = sorted(
        _food_search_bigrams(normalized_query),
        key=lambda item: (-len(item), item),
    )
    if not anchors:
        return []
    params: dict[str, Any] = {
        "candidate_pool_limit": min(max(20, limit * 12), max_rows),
    }
    clauses: list[str] = []
    for index, anchor in enumerate(anchors):
        parameter = f"anchor_{index}"
        clauses.append(f"LOWER(REPLACE(food_name, ' ', '')) LIKE :{parameter}")
        params[parameter] = f"%{anchor}%"
    candidate_rows = (
        connection.execute(
            text(
                f"""
            SELECT food_ref_id, food_name, category, serving_size, energy,
                   carbohydrate, protein, fat, sodium, source, manufacturer
            FROM ai_v13_nutrition_food_ref
            WHERE {" OR ".join(clauses)}
            ORDER BY LENGTH(food_name), LOWER(food_name), food_ref_id
            LIMIT :candidate_pool_limit
            """
            ),
            params,
        )
        .mappings()
        .all()
    )

    query_bigrams = _food_search_bigrams(normalized_query)
    ranked: list[tuple[float, int, str, Any]] = []
    for row in candidate_rows:
        normalized_name = normalize_food_search_text(row["food_name"])
        if not normalized_name:
            continue
        name_bigrams = _food_search_bigrams(normalized_name)
        union = query_bigrams | name_bigrams
        overlap = len(query_bigrams & name_bigrams) / len(union) if union else 0.0
        sequence = SequenceMatcher(
            None,
            normalized_query,
            normalized_name,
            autojunk=False,
        ).ratio()
        containment = float(normalized_query in normalized_name or normalized_name in normalized_query)
        score = (sequence * 0.65) + (overlap * 0.25) + (containment * 0.10)
        if score < 0.32:
            continue
        ranked.append(
            (
                score,
                abs(len(normalized_query) - len(normalized_name)),
                normalized_name,
                row,
            )
        )
    ranked.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def food_record_view(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "food_ref_id": row["food_ref_id"],
        "food_name": row["food_name"],
        "portion": row["portion"],
        "version": row["version"] or 1,
        "nutrients": {
            "칼로리": {"value": row["calories"], "unit": "kcal"},
            "단백질": {"value": row["protein"], "unit": "g"},
            "나트륨": {"value": row["sodium"], "unit": "mg"},
            "지방": {"value": row["fat"], "unit": "g"},
            "탄수화물": {
                "value": row["carbohydrates"],
                "unit": "g",
            },
        },
    }


def food_reference_view(row: Any) -> dict[str, Any]:
    serving_size = _positive_float(row["serving_size"], fallback=100.0)
    serving_multiplier = serving_size / 100.0
    return {
        "food_ref_id": row["food_ref_id"],
        "food_name": row["food_name"],
        "category": row["category"] or "",
        "portion": f"{serving_size:g}g",
        "nutrients": {
            "calories": _scale_reference_nutrient(row["energy"], serving_multiplier),
            "carbohydrates": _scale_reference_nutrient(row["carbohydrate"], serving_multiplier),
            "protein": _scale_reference_nutrient(row["protein"], serving_multiplier),
            "fat": _scale_reference_nutrient(row["fat"], serving_multiplier),
            "sodium": _scale_reference_nutrient(row["sodium"], serving_multiplier),
        },
        "source": row["source"] or "",
        "manufacturer": row["manufacturer"] or "",
    }


def _positive_float(value: Any, *, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _scale_reference_nutrient(
    value: Any,
    multiplier: float,
) -> float | None:
    if value is None:
        return None
    return round(float(value) * multiplier, 6)


def _food_search_bigrams(value: str) -> set[str]:
    if len(value) < 2:
        return {value} if value else set()
    return {value[index : index + 2] for index in range(len(value) - 1)}
