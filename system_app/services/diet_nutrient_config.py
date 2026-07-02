from __future__ import annotations

# 질환 및 CKD 위험도별 한 끼 영양소 제약 한계치.
# 조회 우선순위: (disease, ckd_risk) → (disease, None) → 기본값 없음
NUTRIENT_CONSTRAINT_LIMITS: dict[tuple[str, str | None], dict[str, dict]] = {
    ("kidney_cancer", None): {
        "나트륨":   {"low": 500,  "unit": "mg"},
        "단백질":   {"low": 15,   "unit": "g"},
        "칼로리":   {"moderate_min": 400, "moderate_max": 700, "unit": "kcal"},
        "지방":     {"low": 15,   "unit": "g"},
        "탄수화물": {"low": 80,   "unit": "g"},
    },
    ("kidney_cancer", "low"): {
        "나트륨":   {"low": 500,  "unit": "mg"},
        "단백질":   {"low": 15,   "unit": "g"},
        "칼로리":   {"moderate_min": 400, "moderate_max": 700, "unit": "kcal"},
        "지방":     {"low": 15,   "unit": "g"},
        "탄수화물": {"low": 80,   "unit": "g"},
    },
    ("kidney_cancer", "high"): {
        "나트륨":   {"low": 500,  "unit": "mg"},
        "단백질":   {"low": 13,   "unit": "g"},
        "칼로리":   {"moderate_min": 400, "moderate_max": 700, "unit": "kcal"},
        "지방":     {"low": 15,   "unit": "g"},
        "탄수화물": {"low": 80,   "unit": "g"},
    },
    ("breast_cancer", None): {
        "나트륨":   {"low": 600,  "unit": "mg"},
        "단백질":   {"low": 25,   "unit": "g"},
        "칼로리":   {"moderate_min": 400, "moderate_max": 650, "unit": "kcal"},
        "지방":     {"low": 12,   "unit": "g"},
        "탄수화물": {"low": 90,   "unit": "g"},
    },
}

# NutritionFoodRef 컬럼명 매핑 (한국어 → DB 컬럼)
KOREAN_TO_FOOD_REF_COLUMN: dict[str, str] = {
    "칼로리": "energy",
    "단백질": "protein",
    "나트륨": "sodium",
    "지방": "fat",
    "탄수화물": "carbohydrate",
}

SUPPORTED_CONSTRAINT_LEVELS = {"low", "moderate"}


def get_constraint_limits(disease: str, ckd_risk: str | None) -> dict[str, dict]:
    """질환/CKD 위험도에 맞는 제약 한계치 반환. 없으면 가장 가까운 키로 폴백."""
    key = (disease, ckd_risk)
    if key in NUTRIENT_CONSTRAINT_LIMITS:
        return NUTRIENT_CONSTRAINT_LIMITS[key]
    fallback = (disease, None)
    if fallback in NUTRIENT_CONSTRAINT_LIMITS:
        return NUTRIENT_CONSTRAINT_LIMITS[fallback]
    return {}
