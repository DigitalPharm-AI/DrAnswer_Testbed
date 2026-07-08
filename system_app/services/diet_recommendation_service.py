from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
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
    randomize: bool = True,
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
        serving_col = col * (func.coalesce(NutritionFoodRef.serving_size, 100.0) / 100.0)

        if level == "low" and "low" in nutrient_limits:
            filters.append(col.isnot(None))
            filters.append(serving_col <= nutrient_limits["low"])
            constraints_applied[korean_name] = level
            limits_used[korean_name] = nutrient_limits
        elif level == "moderate" and "moderate_min" in nutrient_limits and "moderate_max" in nutrient_limits:
            filters.append(col.isnot(None))
            filters.append(serving_col >= nutrient_limits["moderate_min"])
            filters.append(serving_col <= nutrient_limits["moderate_max"])
            constraints_applied[korean_name] = level
            limits_used[korean_name] = nutrient_limits

    stmt = select(NutritionFoodRef)
    for f in filters:
        stmt = stmt.where(f)
    if randomize:
        stmt = stmt.order_by(func.random())
    stmt = stmt.limit(limit * 8)  # 선호도 필터링 여유분 확보
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
        annotated["recommendation_reasons"] = _recommendation_reasons(annotated, constraints_applied, limits_used)
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
        "randomized": randomize,
    }


def _row_to_candidate(row: NutritionFoodRef) -> dict[str, Any]:
    serving_size = _serving_size(row)
    multiplier = serving_size / 100.0
    return {
        "food_ref_id": row.food_ref_id,
        "food_name": row.food_name,
        "category": row.category or "",
        "serving_size": serving_size,
        "nutrient_basis": "serving_size",
        "nutrients": {
            "energy":       {"value": _f(row.energy, multiplier),       "unit": "kcal"},
            "protein":      {"value": _f(row.protein, multiplier),      "unit": "g"},
            "sodium":       {"value": _f(row.sodium, multiplier),       "unit": "mg"},
            "fat":          {"value": _f(row.fat, multiplier),          "unit": "g"},
            "carbohydrate": {"value": _f(row.carbohydrate, multiplier), "unit": "g"},
        },
    }


def _serving_size(row: NutritionFoodRef) -> float:
    try:
        value = float(row.serving_size) if row.serving_size is not None else 100.0
    except (TypeError, ValueError):
        return 100.0
    return value if value > 0 else 100.0


def _f(val: float | None, multiplier: float = 1.0) -> float:
    return round(float(val) * multiplier, 2) if val is not None else 0.0


def _recommendation_reasons(
    candidate: dict[str, Any],
    constraints_applied: dict[str, str],
    limits_used: dict[str, Any],
) -> list[str]:
    nutrients = candidate.get("nutrients") if isinstance(candidate.get("nutrients"), dict) else {}
    reasons: list[str] = []
    for korean_name, level in constraints_applied.items():
        nutrient_key = KOREAN_TO_FOOD_REF_COLUMN.get(korean_name)
        nutrient = nutrients.get("energy" if nutrient_key == "energy" else nutrient_key) if nutrient_key else None
        limits = limits_used.get(korean_name) if isinstance(limits_used.get(korean_name), dict) else {}
        if not isinstance(nutrient, dict):
            continue
        value = round(float(nutrient.get("value") or 0), 1)
        unit = str(nutrient.get("unit") or limits.get("unit") or "")
        if level == "low" and "low" in limits:
            reasons.append(f"{korean_name} {value:g}{unit}으로 목표({limits['low']}{unit} 이하)에 맞아요.")
        elif level == "moderate" and "moderate_min" in limits and "moderate_max" in limits:
            reasons.append(
                f"{korean_name} {value:g}{unit}으로 목표 범위({limits['moderate_min']}-{limits['moderate_max']}{unit})에 맞아요."
            )
    preference_match = candidate.get("preference_match") if isinstance(candidate.get("preference_match"), dict) else {}
    if preference_match.get("positive_count"):
        reasons.append("저장된 선호도를 반영한 후보예요.")
    return reasons[:3]
