from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from system_app.models import NutritionFoodRef

logger = logging.getLogger(__name__)

_NUTRIENT_UNITS: dict[str, str] = {
    "energy": "kcal",
    "carbohydrate": "g",
    "protein": "g",
    "fat": "g",
    "sodium": "mg",
}


def search_food_candidates(
    query: str,
    limit: int = 10,
    *,
    session: Session | None,
    patient_id: str | None = None,
) -> list[dict[str, Any]]:
    """음식명으로 후보 목록 반환. 추후 외부 API로 교체 시 이 함수만 변경.

    반환 형태:
        [{food_ref_id, food_name, category, serving_size,
          nutrients: {energy, protein, sodium, fat, carbohydrate: {value, unit}}}]
    """
    if session is None:
        return _fallback_scenario_search(query.strip().lower(), limit, session=None, patient_id=patient_id)
    return _search_from_db(query, limit, session=session, patient_id=patient_id)


def _search_from_db(
    query: str,
    limit: int,
    *,
    session: Session,
    patient_id: str | None,
) -> list[dict[str, Any]]:
    needle = query.strip().lower()
    if not needle:
        return []

    # 테이블이 비어있으면 하드코딩 폴백
    count = session.scalar(select(func.count()).select_from(NutritionFoodRef))
    if not count:
        return _fallback_scenario_search(needle, limit, session=session, patient_id=patient_id)

    rows = session.scalars(
        select(NutritionFoodRef)
        .where(NutritionFoodRef.food_name.like(f"%{needle}%"))
        .limit(limit)
    ).all()

    candidates = [_row_to_candidate(row) for row in rows]

    from system_app.services.nutrition_preference_service import annotate_food_candidate
    return [annotate_food_candidate(session, c, patient_id=patient_id) for c in candidates]


def _row_to_candidate(row: NutritionFoodRef) -> dict[str, Any]:
    nutrients: dict[str, dict[str, Any]] = {}
    for key, unit in _NUTRIENT_UNITS.items():
        value = getattr(row, key, None)
        nutrients[key] = {"value": round(float(value), 2) if value is not None else 0.0, "unit": unit}
    return {
        "food_ref_id": row.food_ref_id,
        "food_name": row.food_name,
        "category": row.category or "",
        "serving_size": float(row.serving_size) if row.serving_size is not None else 100.0,
        "nutrients": nutrients,
    }


def _fallback_scenario_search(
    needle: str,
    limit: int,
    *,
    session: Session | None,
    patient_id: str | None,
) -> list[dict[str, Any]]:
    from system_app.services.nutrition_service import NUTRITION_SCENARIOS, _food_search_candidate

    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for scenario in NUTRITION_SCENARIOS.values():
        for food in scenario["foods"]:
            name = food["food_name"]
            if needle not in name.lower() or name in seen:
                continue
            seen.add(name)
            candidates.append(_food_search_candidate(food))
            if len(candidates) >= limit:
                break
        if len(candidates) >= limit:
            break

    from system_app.services.nutrition_preference_service import annotate_food_candidate
    return [annotate_food_candidate(session, c, patient_id=patient_id) for c in candidates]


def scale_nutrients(nutrients: dict[str, Any], ratio: float) -> dict[str, Any]:
    """영어 키 영양소 dict를 ratio 배율로 스케일링. 구조 유지."""
    result: dict[str, Any] = {}
    for key, entry in nutrients.items():
        if isinstance(entry, dict):
            result[key] = {
                "value": round(float(entry.get("value") or 0) * ratio, 1),
                "unit": entry.get("unit", ""),
            }
    return result


def english_to_korean_nutrients(nutrients: dict[str, Any]) -> dict[str, Any]:
    """영어 키 영양소 dict를 record_meal()이 요구하는 한국어 키로 변환."""
    from system_app.services.nutrition_service import normalize_nutrients
    return normalize_nutrients(nutrients)


def seed_food_ref_from_csv(session: Session, csv_path: str | Path) -> int:
    """nutrition_food_ref 테이블에 CSV 데이터를 삽입. 이미 데이터가 있으면 skip.

    Returns:
        삽입된 행 수 (skip이면 0)
    """
    count = session.scalar(select(func.count()).select_from(NutritionFoodRef))
    if count and count > 0:
        logger.info("nutrition_food_ref already seeded (%d rows), skipping", count)
        return 0

    csv_path = Path(csv_path)
    if not csv_path.exists():
        logger.warning("nutrition_db.csv not found at %s, skipping seed", csv_path)
        return 0

    logger.info("Seeding nutrition_food_ref from %s", csv_path)
    inserted = 0
    chunk: list[NutritionFoodRef] = []

    def _float(val: str) -> float | None:
        stripped = (val or "").strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None

    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            chunk.append(
                NutritionFoodRef(
                    food_ref_id=row.get("food_id", "").strip(),
                    food_name=row.get("food_name", "").strip(),
                    category=row.get("category", "").strip(),
                    serving_size=_float(row.get("serving_size", "")),
                    energy=_float(row.get("energy", "")),
                    carbohydrate=_float(row.get("carbohydrate", "")),
                    protein=_float(row.get("protein", "")),
                    fat=_float(row.get("fat", "")),
                    sodium=_float(row.get("sodium", "")),
                    sugar=_float(row.get("sugar", "")),
                    cholesterol=_float(row.get("cholesterol", "")),
                    moisture=_float(row.get("moisture", "")),
                    source=row.get("source", "").strip(),
                    manufacturer=row.get("manufacturer", "").strip(),
                )
            )
            inserted += 1
            if len(chunk) >= 500:
                session.add_all(chunk)
                session.flush()
                chunk = []
                logger.debug("nutrition_food_ref seeded %d rows so far", inserted)

    if chunk:
        session.add_all(chunk)
        session.flush()

    logger.info("nutrition_food_ref seeding complete: %d rows", inserted)
    return inserted
