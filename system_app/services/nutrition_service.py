from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from typing import Any

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from shared.json_utils import parse_json_object as parse_metadata_json
from shared.settings import get_settings
from system_app.models import DailyNutritionCheck, Notification, NutritionFood, NutritionMeal, NutritionProfile
from system_app.services.clock_service import ensure_clock
from system_app.services.notification_service import create_notification
from system_app.services.timeline_service import add_chat_message

NUTRIENTS = ["칼로리", "단백질", "나트륨", "지방", "탄수화물"]
MEAL_TYPE_LABELS = {
    "breakfast": "아침",
    "lunch": "점심",
    "dinner": "저녁",
    "snack": "간식",
}
NUTRIENT_COLUMNS = {
    "칼로리": ("calories", "kcal"),
    "단백질": ("protein", "g"),
    "나트륨": ("sodium", "mg"),
    "지방": ("fat", "g"),
    "탄수화물": ("carbohydrates", "g"),
}
NUTRIENT_ALIASES = {
    "energy": "칼로리",
    "calories": "칼로리",
    "calorie": "칼로리",
    "protein": "단백질",
    "sodium": "나트륨",
    "fat": "지방",
    "carbohydrate": "탄수화물",
    "carbohydrates": "탄수화물",
    "carbs": "탄수화물",
}

EER_COEFFICIENTS = {
    "male": {"alpha": 662.0, "beta": -9.53, "gamma": 15.91, "delta": 539.6},
    "female": {"alpha": 354.0, "beta": -6.91, "gamma": 9.36, "delta": 726.0},
}
PA_COEFFICIENTS = {
    "male": {"sedentary": 1.00, "low": 1.11, "active": 1.25, "very_active": 1.48},
    "female": {"sedentary": 1.00, "low": 1.12, "active": 1.27, "very_active": 1.45},
}
CALORIES_PER_GRAM = {"단백질": 4, "지방": 9, "탄수화물": 4}


def _food(
    food_id: str,
    name: str,
    portion: str,
    calories: float,
    protein: float,
    sodium: float,
    fat: float,
    carbohydrates: float,
) -> dict[str, Any]:
    return {
        "food_ref_id": food_id,
        "food_name": name,
        "portion": portion,
        "nutrients": {
            "칼로리": {"value": calories, "unit": "kcal"},
            "단백질": {"value": protein, "unit": "g"},
            "나트륨": {"value": sodium, "unit": "mg"},
            "지방": {"value": fat, "unit": "g"},
            "탄수화물": {"value": carbohydrates, "unit": "g"},
        },
    }


NUTRITION_SCENARIOS: dict[str, dict[str, Any]] = {
    "normal_breakfast": {
        "label": "정상 아침",
        "meal_type": "breakfast",
        "description": "현미밥, 두부된장국, 시금치나물",
        "result_label": "아침 기록",
        "foods": [
            _food("F001", "현미밥", "소 (150g)", 220, 5, 3, 1.5, 48),
            _food("F002", "두부된장국", "1그릇", 100, 8, 320, 4, 6),
            _food("F003", "시금치나물", "1접시", 60, 2, 120, 3, 5),
        ],
    },
    "high_sodium_lunch": {
        "label": "나트륨 초과 점심",
        "meal_type": "lunch",
        "description": "짜장면, 탕수육",
        "result_label": "점심 기록 + 영양 알림",
        "foods": [
            _food("F025", "짜장면", "1그릇", 700, 20, 1200, 18, 100),
            _food("F026", "탕수육", "소", 350, 15, 500, 18, 30),
        ],
    },
    "high_fat_dinner": {
        "label": "칼로리/지방 초과 저녁",
        "meal_type": "dinner",
        "description": "콤비네이션피자, 콜라",
        "result_label": "저녁 기록 + 영양 알림",
        "foods": [
            _food("F035", "콤비네이션피자", "2조각", 600, 22, 1100, 26, 65),
            _food("F036", "콜라", "300ml", 130, 0, 15, 0, 33),
        ],
    },
}

NUTRITION_ACTIONS = {
    "missed_meal": {"label": "식사 미기록", "description": "이전 식사가 아직 기록되지 않은 상황", "result_label": "영양 알림 생성"},
    "daily_summary": {"label": "오늘 영양 요약", "description": "오늘 식사와 영양 상태를 채팅에 요약", "result_label": "채팅 요약"},
}


def classify_ckd(egfr: float | None) -> tuple[str, str]:
    if egfr is None:
        return "G3a", "high"
    if egfr >= 90:
        return "G1", "low"
    if egfr >= 60:
        return "G2", "low"
    if egfr >= 45:
        return "G3a", "high"
    if egfr >= 30:
        return "G3b", "high"
    if egfr >= 15:
        return "G4", "high"
    return "G5", "high"


def resolve_patient_id(patient_id: str | None = None) -> str:
    settings = get_settings()
    return patient_id or settings.patient_id


def default_nutrition_profile(patient_id: str) -> NutritionProfile:
    return NutritionProfile(
        patient_id=patient_id,
        name="데모 환자",
        age=55,
        gender="male",
        height=170.0,
        weight=70.0,
        disease="kidney_cancer",
        activity_level="sedentary",
        ckd_stage="G3a",
        ckd_risk="high",
    )


def profile_copy_with_ckd(profile: NutritionProfile, stage: str, risk: str) -> NutritionProfile:
    return NutritionProfile(
        patient_id=profile.patient_id,
        name=profile.name,
        age=profile.age,
        gender=profile.gender,
        height=profile.height,
        weight=profile.weight,
        disease=profile.disease,
        activity_level=profile.activity_level,
        egfr=profile.egfr,
        ckd_stage=stage,
        ckd_risk=risk,
    )


def ensure_nutrition_profile(session: Session, patient_id: str | None = None, *, persist: bool = True) -> NutritionProfile:
    target_patient_id = resolve_patient_id(patient_id)
    profile = session.scalar(select(NutritionProfile).where(NutritionProfile.patient_id == target_patient_id))
    if profile is None:
        profile = default_nutrition_profile(target_patient_id)
        if persist:
            session.add(profile)
            session.flush()
    if not profile.ckd_stage or not profile.ckd_risk:
        stage, risk = classify_ckd(profile.egfr)
        if not persist:
            return profile_copy_with_ckd(profile, stage, risk)
        profile.ckd_stage = stage
        profile.ckd_risk = risk
        session.flush()
    return profile


def profile_payload(profile: NutritionProfile) -> dict[str, Any]:
    return {
        "patient_id": profile.patient_id,
        "name": profile.name,
        "age": profile.age,
        "gender": profile.gender,
        "height": profile.height,
        "weight": profile.weight,
        "disease": profile.disease,
        "activity_level": profile.activity_level,
        "egfr": profile.egfr,
        "ckd_stage": profile.ckd_stage,
        "ckd_risk": profile.ckd_risk,
    }


def calculate_thresholds(profile: NutritionProfile) -> dict[str, dict[str, float | str]]:
    gender = profile.gender if profile.gender in EER_COEFFICIENTS else "male"
    activity = profile.activity_level if profile.activity_level in PA_COEFFICIENTS[gender] else "sedentary"
    height_m = profile.height / 100
    coeff = EER_COEFFICIENTS[gender]
    pa = PA_COEFFICIENTS[gender][activity]
    daily_calories = round(coeff["alpha"] + coeff["beta"] * profile.age + pa * (coeff["gamma"] * profile.weight + coeff["delta"] * height_m), 1)
    daily_protein = round(profile.weight * 1.0, 1)
    daily_sodium = round(profile.weight * 30, 1)
    daily_fat = round((daily_calories * 0.25) / CALORIES_PER_GRAM["지방"], 1)

    if profile.disease == "kidney_cancer" and profile.ckd_risk == "high":
        h = profile.height / 100
        ibw_factor = 22 if gender == "male" else 21
        ibw = h * h * ibw_factor
        daily_protein = round(ibw * 0.8, 1)
        daily_sodium = 2000.0

    fixed_calories = daily_protein * CALORIES_PER_GRAM["단백질"] + daily_fat * CALORIES_PER_GRAM["지방"]
    daily_carbs = round((daily_calories - fixed_calories) / CALORIES_PER_GRAM["탄수화물"], 1)
    return {
        "칼로리": {"value": daily_calories, "unit": "kcal"},
        "단백질": {"value": daily_protein, "unit": "g"},
        "나트륨": {"value": daily_sodium, "unit": "mg"},
        "지방": {"value": daily_fat, "unit": "g"},
        "탄수화물": {"value": daily_carbs, "unit": "g"},
    }


def record_meal(
    session: Session,
    *,
    foods: list[dict[str, Any]],
    meal_type: str,
    patient_id: str | None = None,
    meal_date: date | str | None = None,
    meal_time: str | None = None,
    scenario_key: str = "",
    description: str = "",
    create_alert: bool = True,
    replace_existing: bool = False,
) -> dict[str, Any]:
    if meal_type not in MEAL_TYPE_LABELS:
        raise ValueError("unsupported_meal_type")
    if not foods:
        raise ValueError("foods_required")
    profile = ensure_nutrition_profile(session, patient_id)
    clock = ensure_clock(session)
    target_patient_id = profile.patient_id
    target_date = _coerce_date(meal_date) or clock.current_time.date()
    target_time = meal_time or clock.current_time.strftime("%H:%M:%S")
    meal = (
        existing_scenario_meal(session, target_patient_id, target_date, scenario_key)
        if replace_existing and scenario_key
        else None
    )
    replaced_existing = meal is not None
    if meal is None:
        meal = NutritionMeal(
            patient_id=target_patient_id,
            meal_type=meal_type,
            meal_date=target_date,
            meal_time=target_time,
            scenario_key=scenario_key,
            description=description,
            created_at=clock.current_time,
        )
        session.add(meal)
    else:
        meal.meal_type = meal_type
        meal.meal_date = target_date
        meal.meal_time = target_time
        meal.description = description
    session.flush()
    if replaced_existing:
        session.execute(delete(NutritionFood).where(NutritionFood.meal_id == meal.id))
        session.flush()

    for item in foods:
        normalized = normalize_food_payload(item)
        session.add(
            NutritionFood(
                meal_id=meal.id,
                food_ref_id=normalized.get("food_ref_id", ""),
                food_name=normalized["food_name"],
                portion=normalized.get("portion", "1인분"),
                calories=normalized["nutrients"]["칼로리"]["value"],
                protein=normalized["nutrients"]["단백질"]["value"],
                sodium=normalized["nutrients"]["나트륨"]["value"],
                fat=normalized["nutrients"]["지방"]["value"],
                carbohydrates=normalized["nutrients"]["탄수화물"]["value"],
                created_at=clock.current_time,
            )
        )
    session.flush()
    summary = recalculate_daily_nutrition(session, target_patient_id, target_date)
    summary["preferences"] = _nutrition_preference_summary(session, target_patient_id)
    alert = create_nutrition_alert(session, meal, summary) if create_alert else None
    alert_reused = bool(alert is not None and getattr(alert, "_nutrition_alert_reused", False))
    return {
        "success": True,
        "meal": meal_view(session, meal),
        "daily_summary": summary,
        "alert_created": alert is not None and not alert_reused,
        "alert_id": alert.id if alert is not None else None,
        "replaced_existing": replaced_existing,
        "alert_reused": alert_reused,
    }


def existing_scenario_meal(session: Session, patient_id: str, target_date: date, scenario_key: str) -> NutritionMeal | None:
    return session.scalar(
        select(NutritionMeal)
        .where(
            NutritionMeal.patient_id == patient_id,
            NutritionMeal.meal_date == target_date,
            NutritionMeal.scenario_key == scenario_key,
        )
        .order_by(desc(NutritionMeal.id))
    )


def normalize_food_payload(item: dict[str, Any]) -> dict[str, Any]:
    nutrients = normalize_nutrients(item.get("nutrients") if isinstance(item.get("nutrients"), dict) else item)
    name = str(item.get("food_name") or item.get("name") or "").strip()
    if not name:
        raise ValueError("food_name_required")
    return {
        "food_ref_id": str(item.get("food_ref_id") or item.get("food_id") or ""),
        "food_name": name,
        "portion": str(item.get("portion") or "1인분"),
        "nutrients": nutrients,
    }


def normalize_nutrients(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for source_key, value in raw.items():
        nutrient_name = NUTRIENT_ALIASES.get(str(source_key), str(source_key))
        if nutrient_name not in NUTRIENTS:
            continue
        _, unit = NUTRIENT_COLUMNS[nutrient_name]
        if isinstance(value, dict):
            amount = value.get("value", 0)
            unit = str(value.get("unit") or unit)
        else:
            amount = value
        normalized[nutrient_name] = {"value": _coerce_nutrient_amount(amount), "unit": unit}
    for nutrient_name in NUTRIENTS:
        _, unit = NUTRIENT_COLUMNS[nutrient_name]
        normalized.setdefault(nutrient_name, {"value": 0.0, "unit": unit})
    return normalized


def recalculate_daily_nutrition(session: Session, patient_id: str, target_date: date, *, persist: bool = True) -> dict[str, Any]:
    profile = ensure_nutrition_profile(session, patient_id, persist=persist)
    thresholds = calculate_thresholds(profile)
    meals = meals_for_date(session, patient_id, target_date)
    totals = {nutrient: {"value": 0.0, "unit": NUTRIENT_COLUMNS[nutrient][1]} for nutrient in NUTRIENTS}
    for meal in meals:
        for food in foods_for_meal(session, meal.id):
            totals["칼로리"]["value"] += food.calories
            totals["단백질"]["value"] += food.protein
            totals["나트륨"]["value"] += food.sodium
            totals["지방"]["value"] += food.fat
            totals["탄수화물"]["value"] += food.carbohydrates

    nutrients: dict[str, dict[str, Any]] = {}
    exceeded_nutrients: list[str] = []
    for nutrient in NUTRIENTS:
        intake = round(totals[nutrient]["value"], 1)
        threshold = round(float(thresholds[nutrient]["value"]), 1)
        exceeded = intake > threshold
        excess = round(intake - threshold, 1)
        percentage = round((intake / threshold) * 100, 1) if threshold else 0.0
        nutrients[nutrient] = {
            "intake": intake,
            "threshold": threshold,
            "unit": thresholds[nutrient]["unit"],
            "remaining": round(threshold - intake, 1),
            "exceeded": exceeded,
            "excess_amount": excess,
            "excess_percentage": percentage,
        }
        if exceeded:
            exceeded_nutrients.append(nutrient)

    clock = ensure_clock(session)
    if persist:
        check = session.scalar(
            select(DailyNutritionCheck).where(
                DailyNutritionCheck.patient_id == patient_id,
                DailyNutritionCheck.check_date == target_date,
            )
        )
        if check is None:
            check = DailyNutritionCheck(patient_id=patient_id, check_date=target_date)
            session.add(check)
    else:
        check = DailyNutritionCheck(patient_id=patient_id, check_date=target_date)
    check.total_meals = len(meals)
    check.checked_at = clock.current_time
    _apply_check_values(check, nutrients)
    if persist:
        session.flush()
    return daily_summary_payload(check, nutrients, thresholds, meals)


def _apply_check_values(check: DailyNutritionCheck, nutrients: dict[str, dict[str, Any]]) -> None:
    for nutrient, (column, _) in NUTRIENT_COLUMNS.items():
        data = nutrients[nutrient]
        setattr(check, f"threshold_{column}", data["threshold"])
        setattr(check, f"intake_{column}", data["intake"])
        setattr(check, f"exceeded_{column}", bool(data["exceeded"]))
        setattr(check, f"excess_{column}", data["excess_amount"])


def daily_summary_payload(
    check: DailyNutritionCheck,
    nutrients: dict[str, dict[str, Any]],
    thresholds: dict[str, dict[str, Any]],
    meals: list[NutritionMeal],
) -> dict[str, Any]:
    exceeded = [name for name, data in nutrients.items() if data["exceeded"]]
    return {
        "patient_id": check.patient_id,
        "date": check.check_date.isoformat(),
        "total_meals": len(meals),
        "nutrients": nutrients,
        "thresholds": thresholds,
        "summary": {
            "exceeded_count": len(exceeded),
            "exceeded_nutrients": exceeded,
            "status": "exceeded" if exceeded else "ok",
            "status_label": "초과" if exceeded else "정상",
        },
    }


def create_nutrition_alert(session: Session, meal: NutritionMeal, summary: dict[str, Any]):
    daily_exceeded = summary.get("summary", {}).get("exceeded_nutrients") or []
    if not daily_exceeded:
        return None
    existing = existing_nutrition_alert(
        session,
        patient_id=meal.patient_id,
        target_date=meal.meal_date,
        scenario_key=meal.scenario_key,
        meal_type=meal.meal_type,
    )
    if existing is not None:
        setattr(existing, "_nutrition_alert_reused", True)
        return existing
    clock = ensure_clock(session)
    primary = daily_exceeded[0]
    details = ", ".join(daily_exceeded)
    meal_label = MEAL_TYPE_LABELS.get(meal.meal_type, meal.meal_type)
    body = f"{meal_label} 기록 후 {details} 하루 섭취 기준을 초과했습니다. 채팅에서 조정 방법을 확인할 수 있어요."
    return create_notification(
        session,
        notification_type="nutrition_alert",
        title=f"{primary} 초과 영양 알림",
        body=body,
        visible_at=clock.current_time,
        metadata={
            "category": "nutrition",
            "patient_id": meal.patient_id,
            "meal_date": meal.meal_date.isoformat(),
            "meal_type": meal.meal_type,
            "meal_id": meal.id,
            "scenario_key": meal.scenario_key,
            "exceeded_nutrients": daily_exceeded,
            "severity": "warning",
        },
        patient_id=meal.patient_id,
    )


def existing_nutrition_alert(
    session: Session,
    *,
    patient_id: str,
    target_date: date,
    scenario_key: str,
    meal_type: str | None = None,
) -> Notification | None:
    rows = session.scalars(
        select(Notification)
        .where(
            Notification.patient_id == patient_id,
            Notification.notification_type == "nutrition_alert",
            Notification.acknowledged.is_(False),
        )
        .order_by(desc(Notification.visible_at), desc(Notification.id))
        .limit(50)
    ).all()
    for row in rows:
        metadata = parse_metadata_json(row.metadata_json)
        if metadata.get("category") != "nutrition":
            continue
        if metadata.get("scenario_key") != scenario_key:
            continue
        if metadata.get("meal_date") != target_date.isoformat():
            continue
        if meal_type is not None and metadata.get("meal_type") != meal_type:
            continue
        return row
    return None


def create_missing_meal_alert(session: Session, meal_type: str = "lunch", scenario_key: str = "missed_meal", patient_id: str | None = None):
    clock = ensure_clock(session)
    target_patient_id = resolve_patient_id(patient_id)
    existing = existing_nutrition_alert(
        session,
        patient_id=target_patient_id,
        target_date=clock.current_time.date(),
        scenario_key=scenario_key,
        meal_type=meal_type,
    )
    if existing is not None:
        setattr(existing, "_nutrition_alert_reused", True)
        return existing
    meal_label = MEAL_TYPE_LABELS.get(meal_type, meal_type)
    return create_notification(
        session,
        notification_type="nutrition_alert",
        title="식사 기록 확인",
        body=f"{meal_label} 식사가 아직 기록되지 않았습니다. 드신 음식이 있으면 채팅에서 알려주세요.",
        visible_at=clock.current_time,
        metadata={
            "category": "nutrition",
            "patient_id": target_patient_id,
            "meal_date": clock.current_time.date().isoformat(),
            "scenario_key": scenario_key,
            "meal_type": meal_type,
            "severity": "reminder",
        },
        patient_id=target_patient_id,
    )


def run_nutrition_scenario(session: Session, scenario_key: str, patient_id: str | None = None) -> dict[str, Any]:
    clock = ensure_clock(session)
    target_patient_id = resolve_patient_id(patient_id)
    if scenario_key in NUTRITION_SCENARIOS:
        scenario = NUTRITION_SCENARIOS[scenario_key]
        add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="nutrition",
            content=f"가상 영양 입력: {scenario['label']} ({scenario['description']})",
            patient_id=target_patient_id,
        )
        result = record_meal(
            session,
            foods=scenario["foods"],
            meal_type=scenario["meal_type"],
            patient_id=target_patient_id,
            meal_date=clock.current_time.date(),
            meal_time=clock.current_time.strftime("%H:%M:%S"),
            scenario_key=scenario_key,
            description=scenario["description"],
            replace_existing=True,
        )
        message = nutrition_summary_text(result["daily_summary"])
        if result.get("alert_reused"):
            message += "\n이미 같은 시나리오 알림이 있어 기존 알림을 유지했어요."
        add_chat_message(session, role="assistant", sender_type="assistant", category="nutrition", content=message, patient_id=target_patient_id)
        return {"scenario_key": scenario_key, "message": message, **result}

    if scenario_key == "missed_meal":
        add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="nutrition",
            content="가상 영양 입력: 식사 미기록 상황",
            patient_id=target_patient_id,
        )
        notification = create_missing_meal_alert(session, patient_id=target_patient_id)
        alert_reused = bool(getattr(notification, "_nutrition_alert_reused", False))
        message = "점심 식사가 아직 기록되지 않은 상황을 만들었어요. 알림 센터와 채팅에서 이어서 확인할 수 있습니다."
        if alert_reused:
            message += "\n이미 같은 식사 미기록 알림이 있어 기존 알림을 유지했어요."
        add_chat_message(session, role="assistant", sender_type="assistant", category="nutrition", content=message, patient_id=target_patient_id)
        return {
            "scenario_key": scenario_key,
            "success": True,
            "message": message,
            "alert_created": not alert_reused,
            "alert_reused": alert_reused,
            "alert_id": notification.id,
        }

    if scenario_key == "daily_summary":
        summary = daily_nutrition_view(session, patient_id=target_patient_id)
        add_chat_message(
            session,
            role="user",
            sender_type="patient",
            category="nutrition",
            content="오늘 영양 상태를 요약해줘.",
            patient_id=target_patient_id,
        )
        message = nutrition_summary_text(summary)
        add_chat_message(session, role="assistant", sender_type="assistant", category="nutrition", content=message, patient_id=target_patient_id)
        return {"scenario_key": scenario_key, "success": True, "message": message, "daily_summary": summary}

    raise ValueError(f"unknown_nutrition_scenario:{scenario_key}")


def daily_nutrition_view(
    session: Session,
    target_date: date | str | None = None,
    patient_id: str | None = None,
    *,
    persist: bool = False,
) -> dict[str, Any]:
    profile = ensure_nutrition_profile(session, patient_id, persist=persist)
    clock = ensure_clock(session)
    actual_date = _coerce_date(target_date) or clock.current_time.date()
    meals = meals_for_date(session, profile.patient_id, actual_date)
    if meals:
        summary = recalculate_daily_nutrition(session, profile.patient_id, actual_date, persist=persist)
        summary["preferences"] = _nutrition_preference_summary(session, profile.patient_id)
        return summary
    thresholds = calculate_thresholds(profile)
    nutrients = {
        nutrient: {
            "intake": 0.0,
            "threshold": threshold["value"],
            "unit": threshold["unit"],
            "remaining": threshold["value"],
            "exceeded": False,
            "excess_amount": round(-float(threshold["value"]), 1),
            "excess_percentage": 0.0,
        }
        for nutrient, threshold in thresholds.items()
    }
    check = DailyNutritionCheck(patient_id=profile.patient_id, check_date=actual_date, total_meals=0, checked_at=clock.current_time)
    summary = daily_summary_payload(check, nutrients, thresholds, [])
    summary["preferences"] = _nutrition_preference_summary(session, profile.patient_id)
    return summary


def nutrition_dashboard_view(session: Session, patient_id: str | None = None) -> dict[str, Any]:
    profile = ensure_nutrition_profile(session, patient_id, persist=False)
    summary = daily_nutrition_view(session, patient_id=profile.patient_id)
    meals = [meal_view(session, meal) for meal in meals_for_date(session, profile.patient_id, date.fromisoformat(summary["date"]))]
    metric_views = nutrition_metric_views(summary, meals)
    recorded_scenarios = {meal["scenario_key"] for meal in meals if meal.get("scenario_key")}
    preferences = _nutrition_preference_summary(session, profile.patient_id)
    return {
        "profile": profile_payload(profile),
        "summary": summary,
        "preferences": preferences,
        "metrics": metric_views,
        "meals": meals,
        "scenarios": [
            {
                "key": key,
                "label": scenario["label"],
                "description": scenario["description"],
                "result_label": scenario.get("result_label", "식사 기록"),
                "is_recorded": key in recorded_scenarios,
            }
            for key, scenario in NUTRITION_SCENARIOS.items()
        ]
        + [
            {
                "key": key,
                "label": value["label"],
                "description": value["description"],
                "result_label": value.get("result_label", "실행"),
                "is_recorded": False,
            }
            for key, value in NUTRITION_ACTIONS.items()
        ],
    }


def nutrition_metric_views(summary: dict[str, Any], meals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    nutrients = summary.get("nutrients") if isinstance(summary.get("nutrients"), dict) else {}

    metrics: list[dict[str, Any]] = []
    for nutrient in NUTRIENTS:
        item = nutrients.get(nutrient, {})
        threshold = float(item.get("threshold") or 0)
        intake = float(item.get("intake") or 0)
        percent = round((intake / threshold) * 100, 1) if threshold else 0.0
        daily_exceeded = bool(item.get("exceeded"))
        basis_label = "일일 기준 초과" if daily_exceeded else "기준 내"
        risk_rank = 0 if daily_exceeded else 1
        metrics.append(
            {
                "name": nutrient,
                "intake": round(intake, 1),
                "threshold": round(threshold, 1),
                "unit": item.get("unit", NUTRIENT_COLUMNS[nutrient][1]),
                "remaining": round(float(item.get("remaining") or 0), 1),
                "percent": percent,
                "bar_percent": min(100.0, max(0.0, percent)),
                "daily_exceeded": daily_exceeded,
                "meal_exceeded": False,
                "is_warning": daily_exceeded,
                "basis_label": basis_label,
                "risk_rank": risk_rank,
            }
        )
    return sorted(metrics, key=lambda item: (item["risk_rank"], -item["percent"], item["name"]))


def meals_for_date(session: Session, patient_id: str, target_date: date) -> list[NutritionMeal]:
    return list(
        session.scalars(
            select(NutritionMeal)
            .where(NutritionMeal.patient_id == patient_id, NutritionMeal.meal_date == target_date)
            .order_by(NutritionMeal.meal_time.asc(), NutritionMeal.id.asc())
        ).all()
    )


def foods_for_meal(session: Session, meal_id: int) -> list[NutritionFood]:
    return list(session.scalars(select(NutritionFood).where(NutritionFood.meal_id == meal_id).order_by(NutritionFood.id.asc())).all())


def meal_view(session: Session, meal: NutritionMeal) -> dict[str, Any]:
    foods = [food_view(food) for food in foods_for_meal(session, meal.id)]
    return {
        "id": meal.id,
        "patient_id": meal.patient_id,
        "meal_type": meal.meal_type,
        "meal_label": MEAL_TYPE_LABELS.get(meal.meal_type, meal.meal_type),
        "meal_date": meal.meal_date.isoformat(),
        "meal_time": meal.meal_time,
        "scenario_key": meal.scenario_key,
        "description": meal.description,
        "foods": foods,
    }


def food_view(food: NutritionFood) -> dict[str, Any]:
    return {
        "id": food.id,
        "food_ref_id": food.food_ref_id,
        "food_name": food.food_name,
        "portion": food.portion,
        "nutrients": {
            "칼로리": {"value": food.calories, "unit": "kcal"},
            "단백질": {"value": food.protein, "unit": "g"},
            "나트륨": {"value": food.sodium, "unit": "mg"},
            "지방": {"value": food.fat, "unit": "g"},
            "탄수화물": {"value": food.carbohydrates, "unit": "g"},
        },
    }


def search_foods(query: str, limit: int = 10, *, session: Session | None = None, patient_id: str | None = None) -> dict[str, Any]:
    needle = query.strip().lower()
    if not needle:
        return {"success": False, "error": "query_required", "candidates": []}
    from system_app.services.food_search_service import search_food_candidates
    candidates = search_food_candidates(needle, limit, session=session, patient_id=patient_id)
    return {"success": True, "candidates": candidates, "source": "db" if candidates else "sample"}


def _food_search_candidate(food: dict[str, Any]) -> dict[str, Any]:
    nutrients = food["nutrients"]
    return {
        "food_ref_id": food.get("food_ref_id", ""),
        "food_name": food["food_name"],
        "category": "sample",
        "serving_size": 100,
        "portion": food.get("portion", "1인분"),
        "nutrients": {
            "energy": nutrients["칼로리"],
            "protein": nutrients["단백질"],
            "sodium": nutrients["나트륨"],
            "fat": nutrients["지방"],
            "carbohydrate": nutrients["탄수화물"],
        },
    }


def nutrition_summary_text(summary: dict[str, Any]) -> str:
    date_label = summary.get("date", "")
    total_meals = summary.get("total_meals", 0)
    nutrients = summary.get("nutrients") if isinstance(summary.get("nutrients"), dict) else {}
    exceeded = summary.get("summary", {}).get("exceeded_nutrients") or []
    lines = [f"{date_label} 영양 요약: 식사 {total_meals}건 기록."]
    for nutrient in ["칼로리", "단백질", "나트륨", "지방"]:
        data = nutrients.get(nutrient, {})
        lines.append(
            f"{nutrient} {data.get('intake', 0)}{data.get('unit', '')} / 기준 {data.get('threshold', 0)}{data.get('unit', '')}"
        )
    if exceeded:
        lines.append(f"초과 항목: {', '.join(exceeded)}. 다음 식사는 나트륨과 지방을 낮춘 선택이 좋아요.")
    else:
        lines.append("현재까지 초과 항목은 없습니다.")
    return "\n".join(lines)


def build_nutrition_context(session: Session, patient_id: str | None = None) -> dict[str, Any]:
    dashboard = nutrition_dashboard_view(session, patient_id=patient_id)
    meals = dashboard["meals"]
    summary = dashboard["summary"]
    return {
        "profile": dashboard["profile"],
        "today_summary": summary,
        "preferences": dashboard["preferences"],
        "today_meals": [
            {
                "meal_id": meal["id"],
                "meal_type": meal["meal_type"],
                "meal_label": meal["meal_label"],
                "foods": [{"food_name": food["food_name"], "portion": food["portion"]} for food in meal["foods"]],
            }
            for meal in meals
        ],
    }


def _nutrition_preference_summary(session: Session, patient_id: str) -> dict[str, Any]:
    from system_app.services.nutrition_preference_service import nutrition_preference_summary

    return nutrition_preference_summary(session, patient_id=patient_id)


def _annotate_food_candidate(session: Session | None, candidate: dict[str, Any], patient_id: str | None) -> dict[str, Any]:
    from system_app.services.nutrition_preference_service import annotate_food_candidate

    return annotate_food_candidate(session, candidate, patient_id=patient_id)


def _coerce_date(value: date | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("invalid_nutrition_date") from exc


def _coerce_nutrient_amount(value: Any) -> float:
    try:
        amount = float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_nutrient_value") from exc
    if not isfinite(amount):
        raise ValueError("invalid_nutrient_value")
    if amount < 0:
        raise ValueError("negative_nutrient_value")
    return round(amount, 2)
