from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from system_app.models import NutritionFoodRef
from system_app.services.diet_nutrient_config import (
    KOREAN_TO_FOOD_REF_COLUMN,
    SUPPORTED_CONSTRAINT_LEVELS,
    get_constraint_limits,
)
from system_app.services.nutrition_service import ensure_nutrition_profile


def recommend_diet(
    session: Session,
    *,
    patient_id: str | None = None,
    constraints: dict[str, str],
    meal_type: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """영양 제약 조건에 맞는 음식 추천.

    constraints: 한국어 영양소명 → "low" | "moderate"
    """
    if not constraints:
        raise ValueError("constraints_required")

    invalid = {k: v for k, v in constraints.items() if v not in SUPPORTED_CONSTRAINT_LEVELS}
    if invalid:
        raise ValueError(f"invalid_constraint_level:{list(invalid.keys())}")

    profile = ensure_nutrition_profile(session, patient_id, persist=False)
    disease = profile.disease or ""
    ckd_risk = profile.ckd_risk or None

    limits = get_constraint_limits(disease, ckd_risk)

    # SQLAlchemy 필터 구성
    filters = []
    constraints_applied: dict[str, str] = {}
    limits_used: dict[str, Any] = {}

    for korean_name, level in constraints.items():
        col_name = KOREAN_TO_FOOD_REF_COLUMN.get(korean_name)
        if col_name is None:
            continue
        nutrient_limits = limits.get(korean_name)
        if not nutrient_limits:
            continue

        col = getattr(NutritionFoodRef, col_name, None)
        if col is None:
            continue

        if level == "low" and "low" in nutrient_limits:
            filters.append(col.isnot(None))
            filters.append(col <= nutrient_limits["low"])
            constraints_applied[korean_name] = level
            limits_used[korean_name] = nutrient_limits
        elif level == "moderate" and "moderate_min" in nutrient_limits and "moderate_max" in nutrient_limits:
            filters.append(col.isnot(None))
            filters.append(col >= nutrient_limits["moderate_min"])
            filters.append(col <= nutrient_limits["moderate_max"])
            constraints_applied[korean_name] = level
            limits_used[korean_name] = nutrient_limits

    stmt = select(NutritionFoodRef)
    for f in filters:
        stmt = stmt.where(f)
    stmt = stmt.limit(limit * 4)  # 선호도 필터링 여유분 확보

    rows = session.scalars(stmt).all()

    from system_app.services.nutrition_preference_service import annotate_food_candidate

    recommendations: list[dict[str, Any]] = []
    blocked_count = 0

    for row in rows:
        candidate = _row_to_candidate(row)
        annotated = annotate_food_candidate(session, candidate, patient_id=profile.patient_id)
        if annotated.get("preference_match", {}).get("status") == "blocked":
            blocked_count += 1
            continue
        recommendations.append(annotated)
        if len(recommendations) >= limit:
            break

    return {
        "success": True,
        "disease": disease,
        "ckd_risk": ckd_risk or "",
        "constraints_applied": constraints_applied,
        "limits_used": limits_used,
        "recommendations": recommendations,
        "blocked_count": blocked_count,
        "total_candidates": len(rows),
    }


def _row_to_candidate(row: NutritionFoodRef) -> dict[str, Any]:
    return {
        "food_ref_id": row.food_ref_id,
        "food_name": row.food_name,
        "category": row.category or "",
        "serving_size": float(row.serving_size) if row.serving_size is not None else 100.0,
        "nutrients": {
            "energy":       {"value": _f(row.energy),       "unit": "kcal"},
            "protein":      {"value": _f(row.protein),      "unit": "g"},
            "sodium":       {"value": _f(row.sodium),       "unit": "mg"},
            "fat":          {"value": _f(row.fat),          "unit": "g"},
            "carbohydrate": {"value": _f(row.carbohydrate), "unit": "g"},
        },
    }


def _f(val: float | None) -> float:
    return round(float(val), 2) if val is not None else 0.0
