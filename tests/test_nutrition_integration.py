import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from system_app.db import SessionLocal
from system_app.main import app
from system_app.models import (
    ChatMessage,
    DailyNutritionCheck,
    Notification,
    NutritionFood,
    NutritionFoodRef,
    NutritionMeal,
    NutritionOntologyNode,
    NutritionOntologyTriple,
    NutritionPatientPreferenceTriple,
)
from system_app.services.food_search_service import english_to_korean_nutrients, scale_nutrients
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.nutrition_preference_service import (
    nutrition_preference_summary,
    parse_preference_csv,
    record_preference_csv_lists,
    record_preference_fact,
    seed_nutrition_ontology,
)
from system_app.services.nutrition_service import (
    daily_nutrition_view,
    delete_food,
    delete_meal,
    nutrition_dashboard_view,
    record_meal,
    run_nutrition_scenario,
    search_foods,
    update_food,
    update_meal,
)
from tests.helpers import build_session


def reset_testbed(client: TestClient) -> None:
    response = client.post(
        "/api/ui/v1/testbed/reset",
        json={
            "request_id": f"req_{uuid4().hex[:16]}",
            "confirm": True,
        },
    )
    assert response.status_code == 200


def test_nutrition_scenario_records_daily_threshold_alert():
    with build_session() as session:
        breakfast = run_nutrition_scenario(session, "normal_breakfast")
        result = run_nutrition_scenario(session, "high_sodium_lunch")
        session.commit()

        assert breakfast["success"] is True
        assert breakfast["alert_created"] is False
        assert result["success"] is True
        assert result["alert_created"] is True
        assert session.query(NutritionMeal).count() == 2
        assert session.query(NutritionFood).count() == 5

        notification = session.query(Notification).filter(Notification.notification_type == "nutrition_alert").one()
        metadata = json.loads(notification.metadata_json)
        assert metadata["category"] == "nutrition"
        assert "나트륨" in metadata["exceeded_nutrients"]
        assert "하루 섭취 기준" in notification.body

        messages = session.query(ChatMessage).filter(ChatMessage.category == "nutrition").all()
        assert len(messages) == 4
        assert "가상 영양 입력" in messages[-2].content
        assert "영양 요약" in messages[-1].content


def test_nutrition_scenario_rerun_replaces_meal_and_reuses_daily_alert():
    with build_session() as session:
        breakfast = run_nutrition_scenario(session, "normal_breakfast")
        first = run_nutrition_scenario(session, "high_sodium_lunch")
        second = run_nutrition_scenario(session, "high_sodium_lunch")
        session.commit()

        assert breakfast["alert_created"] is False
        assert first["alert_created"] is True
        assert second["replaced_existing"] is True
        assert second["alert_created"] is False
        assert second["alert_reused"] is True
        assert session.query(NutritionMeal).count() == 2
        assert session.query(NutritionFood).count() == 5
        assert session.query(Notification).filter(Notification.notification_type == "nutrition_alert").count() == 1


def test_simulation_reset_clears_nutrition_runtime_rows():
    with build_session() as session:
        run_nutrition_scenario(session, "high_sodium_lunch")
        session.commit()

        assert session.query(NutritionMeal).count() == 1
        assert session.query(NutritionFood).count() == 2
        assert session.query(DailyNutritionCheck).count() == 1

        reset_simulation_state(session)

        assert session.query(NutritionMeal).count() == 0
        assert session.query(NutritionFood).count() == 0
        assert session.query(DailyNutritionCheck).count() == 0


def test_simulation_reset_clears_nutrition_preferences_and_allergies():
    patient_id = "reset-preferences-patient"
    with build_session() as session:
        record_preference_fact(
            session,
            patient_id=patient_id,
            predicate="likes",
            object_label="preferred-food",
        )
        record_preference_fact(
            session,
            patient_id=patient_id,
            predicate="allergic_to",
            object_label="soy-allergen",
            object_type="allergen",
        )
        session.commit()

        assert session.query(NutritionPatientPreferenceTriple).count() == 2

        reset_simulation_state(session)

        assert session.query(NutritionPatientPreferenceTriple).count() == 0
        assert nutrition_preference_summary(session, patient_id=patient_id)["counts"] == {
            "hard": 0,
            "soft": 0,
            "total": 0,
        }


def test_nutrition_views_are_scoped_by_patient_id():
    foods = [
        {
            "food_name": "테스트 식사",
            "nutrients": {"calories": 100, "protein": 4, "sodium": 100, "fat": 2, "carbohydrates": 12},
        }
    ]
    with build_session() as session:
        record_meal(session, patient_id="patient-a", foods=foods, meal_type="breakfast", create_alert=False)
        record_meal(session, patient_id="patient-b", foods=foods, meal_type="lunch", create_alert=False)
        session.commit()

        summary_a = daily_nutrition_view(session, patient_id="patient-a")
        summary_b = daily_nutrition_view(session, patient_id="patient-b")

        assert summary_a["patient_id"] == "patient-a"
        assert summary_b["patient_id"] == "patient-b"
        assert session.query(NutritionMeal).filter(NutritionMeal.patient_id == "patient-a").count() == 1
        assert session.query(NutritionMeal).filter(NutritionMeal.patient_id == "patient-b").count() == 1


def test_food_search_serving_size_nutrients_scale_before_meal_recording():
    with build_session() as session:
        session.add(
            NutritionFoodRef(
                food_ref_id="serving-scale-food",
                food_name="기준량 테스트 음식",
                category="테스트",
                serving_size=200,
                energy=100,
                protein=5,
                sodium=20,
                fat=3,
                carbohydrate=10,
            )
        )
        session.commit()

        result = search_foods("기준량 테스트", session=session, patient_id="patient-serving-scale")
        candidate = result["candidates"][0]
        assert candidate["serving_size"] == 200
        assert candidate["nutrients"]["energy"]["value"] == 200
        assert candidate["nutrients"]["protein"]["value"] == 10
        assert candidate["nutrients"]["sodium"]["value"] == 40

        scaled = scale_nutrients(candidate["nutrients"], 0.5)
        record_meal(
            session,
            patient_id="patient-serving-scale",
            meal_type="breakfast",
            foods=[
                {
                    "food_ref_id": candidate["food_ref_id"],
                    "food_name": candidate["food_name"],
                    "portion": "100g",
                    "nutrients": english_to_korean_nutrients(scaled),
                }
            ],
            create_alert=False,
        )
        summary = daily_nutrition_view(session, patient_id="patient-serving-scale")

        assert summary["nutrients"]["칼로리"]["intake"] == 100
        assert summary["nutrients"]["단백질"]["intake"] == 5
        assert summary["nutrients"]["나트륨"]["intake"] == 20


def test_nutrition_dashboard_exposes_portions_and_missing_reference_nutrients():
    with build_session() as session:
        session.add(
            NutritionFoodRef(
                food_ref_id="missing-nutrients-food",
                food_name="영양정보 누락 음식",
                category="테스트",
                serving_size=1008,
                energy=306,
                protein=12.7,
                sodium=413,
                fat=None,
                carbohydrate=None,
            )
        )
        session.commit()

        candidate = search_foods(
            "영양정보 누락",
            session=session,
            patient_id="patient-missing-nutrients",
        )["candidates"][0]
        record_meal(
            session,
            patient_id="patient-missing-nutrients",
            meal_type="dinner",
            foods=[
                {
                    "food_ref_id": candidate["food_ref_id"],
                    "food_name": candidate["food_name"],
                    "portion": "252g",
                    "nutrients": english_to_korean_nutrients(
                        scale_nutrients(candidate["nutrients"], 0.25)
                    ),
                }
            ],
            create_alert=False,
        )

        dashboard = nutrition_dashboard_view(
            session,
            patient_id="patient-missing-nutrients",
        )
        food = dashboard["meals"][0]["foods"][0]

        assert food["reference_portion"] == "1008g"
        assert food["portion"] == "252g"
        assert food["nutrients"]["칼로리"]["available"] is True
        assert food["nutrients"]["탄수화물"] == {
            "value": 0.0,
            "unit": "g",
            "available": False,
        }
        assert food["nutrients"]["지방"]["available"] is False
def test_preference_csv_parser_accepts_comma_lists_without_brackets():
    assert parse_preference_csv("현미밥, 연어, 두부") == ["현미밥", "연어", "두부"]
    assert parse_preference_csv("현미밥,, 연어, 현미밥") == ["현미밥", "연어"]
    assert parse_preference_csv("(짜장면), [매운 음식]") == ["짜장면", "매운 음식"]


def test_legacy_agent_nutrition_search_api_is_removed():
    client = TestClient(app)

    search_response = client.post("/api/agent/nutrition/food/search", json={"query": "짜장"})

    assert search_response.status_code == 404


def test_food_search_uses_deterministic_relevance_before_limit():
    with build_session() as session:
        session.add_all(
            [
                NutritionFoodRef(
                    food_ref_id="food-substring",
                    food_name="오징어_짜장",
                ),
                NutritionFoodRef(
                    food_ref_id="food-long-prefix",
                    food_name="짜장밥_오징어",
                ),
                NutritionFoodRef(
                    food_ref_id="food-short-prefix",
                    food_name="짜장면",
                ),
                NutritionFoodRef(
                    food_ref_id="food-exact",
                    food_name="짜장",
                ),
            ]
        )
        session.commit()

        exact = search_foods("짜장", limit=1, session=session)
        session.delete(session.get(NutritionFoodRef, "food-exact"))
        session.commit()
        shortest_prefix = search_foods("짜장", limit=1, session=session)

        assert exact["candidates"][0]["food_name"] == "짜장"
        assert shortest_prefix["candidates"][0]["food_name"] == "짜장면"


def test_nutrition_service_writes_keep_meal_records_patient_scoped():
    client = TestClient(app)
    reset_testbed(client)
    meal_payload = {
        "foods": [
            {
                "food_name": "scope test meal",
                "portion": "1 bowl",
                "nutrients": {"calories": 100, "protein": 4, "sodium": 100, "fat": 2, "carbohydrates": 12},
            }
        ],
        "meal_date": "2026-04-20",
        "meal_time": "08:30:00",
        "scenario_key": "scope-test",
        "description": "patient scope check",
    }

    with SessionLocal() as session:
        record_meal(
            session,
            **meal_payload,
            patient_id="patient-a",
            meal_type="breakfast",
            create_alert=False,
        )
        record_meal(
            session,
            **meal_payload,
            patient_id="patient-b",
            meal_type="lunch",
            create_alert=False,
        )
        session.commit()
        patient_a_meals = session.query(NutritionMeal).filter(
            NutritionMeal.patient_id == "patient-a"
        ).all()
        patient_b_meals = session.query(NutritionMeal).filter(
            NutritionMeal.patient_id == "patient-b"
        ).all()
        summary_b = daily_nutrition_view(
            session,
            "2026-04-20",
            patient_id="patient-b",
        )
    assert {meal.patient_id for meal in patient_a_meals} == {"patient-a"}
    assert {meal.patient_id for meal in patient_b_meals} == {"patient-b"}
    assert summary_b["patient_id"] == "patient-b"
    assert summary_b["total_meals"] == 1


def test_nutrition_service_updates_and_deletes_meal_records():
    client = TestClient(app)
    reset_testbed(client)
    meal_payload = {
        "patient_id": "patient-crud",
        "meal_type": "lunch",
        "meal_date": "2026-04-20",
        "meal_time": "12:00:00",
        "foods": [
            {
                "food_name": "crud original meal",
                "portion": "1 bowl",
                "nutrients": {"calories": 100, "protein": 4, "sodium": 100, "fat": 2, "carbohydrates": 12},
            }
        ],
    }

    with SessionLocal() as session:
        created = record_meal(
            session,
            patient_id=meal_payload["patient_id"],
            meal_type=meal_payload["meal_type"],
            meal_date=meal_payload["meal_date"],
            meal_time=meal_payload["meal_time"],
            foods=meal_payload["foods"],
            create_alert=False,
        )
        meal_id = created["meal"]["id"]
        update_result = update_meal(
            session,
            meal_id=meal_id,
            patient_id="patient-crud",
            meal_type="dinner",
            description="corrected meal",
            foods=[
                {
                    "food_name": "crud updated meal",
                    "portion": "2 pieces",
                    "nutrients": {
                        "calories": 180,
                        "protein": 10,
                        "sodium": 150,
                        "fat": 4,
                        "carbohydrates": 20,
                    },
                }
            ],
            reason="patient correction",
            create_alert=False,
        )
        with pytest.raises(
            ValueError,
            match="nutrition_meal_not_found",
        ):
            delete_meal(
                session,
                meal_id=meal_id,
                patient_id="other-patient",
                reason="wrong patient should not delete",
            )
        delete_result = delete_meal(
            session,
            meal_id=meal_id,
            patient_id="patient-crud",
            reason="patient requested delete",
        )
        session.commit()
        updated = update_result["meal"]
        assert updated["id"] == meal_id
        assert updated["meal_type"] == "dinner"
        assert updated["description"] == "corrected meal"
        assert updated["foods"][0]["food_name"] == "crud updated meal"
        assert delete_result["deleted_meal"]["id"] == meal_id
        assert (
            session.query(NutritionMeal)
            .filter(NutritionMeal.patient_id == "patient-crud")
            .count()
            == 0
        )


def test_nutrition_service_updates_and_deletes_food_records():
    client = TestClient(app)
    reset_testbed(client)
    meal_payload = {
        "patient_id": "patient-food-crud",
        "meal_type": "lunch",
        "meal_date": "2026-04-20",
        "meal_time": "12:00:00",
        "foods": [
            {
                "food_name": "탕수육",
                "portion": "1 plate",
                "nutrients": {"calories": 400, "protein": 15, "sodium": 500, "fat": 20, "carbohydrates": 40},
            },
            {
                "food_name": "밥",
                "portion": "1 bowl",
                "nutrients": {"calories": 300, "protein": 6, "sodium": 10, "fat": 1, "carbohydrates": 65},
            },
        ],
    }

    with SessionLocal() as session:
        created = record_meal(
            session,
            patient_id=meal_payload["patient_id"],
            meal_type=meal_payload["meal_type"],
            meal_date=meal_payload["meal_date"],
            meal_time=meal_payload["meal_time"],
            foods=meal_payload["foods"],
            create_alert=False,
        )
        meal = created["meal"]
        meal_id = meal["id"]
        first_food_id = meal["foods"][0]["id"]
        second_food_id = meal["foods"][1]["id"]
        updated_payload = update_food(
            session,
            meal_id=meal_id,
            food_id=first_food_id,
            patient_id="patient-food-crud",
            food_ref_id="guobaorou-ref",
            food_name="꿔바로우",
            portion="1 plate",
            nutrients={
                "calories": 450,
                "protein": 16,
                "sodium": 550,
                "fat": 22,
                "carbohydrates": 44,
            },
            reason="patient corrected food",
        )
        with pytest.raises(
            ValueError,
            match="nutrition_meal_not_found",
        ):
            delete_food(
                session,
                meal_id=meal_id,
                food_id=second_food_id,
                patient_id="other-patient",
                reason="wrong patient should not delete",
            )
        delete_result = delete_food(
            session,
            meal_id=meal_id,
            food_id=second_food_id,
            patient_id="patient-food-crud",
            reason="patient removed rice",
        )
        delete_last_result = delete_food(
            session,
            meal_id=meal_id,
            food_id=first_food_id,
            patient_id="patient-food-crud",
            reason="patient removed last food",
        )
        session.commit()
        assert updated_payload["food"]["id"] == first_food_id
        assert updated_payload["food"]["food_name"] == "꿔바로우"
        assert updated_payload["food"]["food_ref_id"] == "guobaorou-ref"
        assert updated_payload["meal"]["foods"][0]["food_name"] == "꿔바로우"
        assert delete_result["deleted_food"]["id"] == second_food_id
        assert delete_result["meal_deleted"] is False
        assert len(delete_result["meal"]["foods"]) == 1
        assert delete_last_result["meal_deleted"] is True
        assert delete_last_result["meal"] is None
        assert (
            session.query(NutritionMeal)
            .filter(NutritionMeal.patient_id == "patient-food-crud")
            .count()
            == 0
        )


def test_direct_agent_nutrition_write_routes_are_removed():
    client = TestClient(app)
    paths = [
        "/api/agent/nutrition/meals",
        "/api/agent/nutrition/meals/meal-public-id/update",
        "/api/agent/nutrition/meals/meal-public-id/delete",
        (
            "/api/agent/nutrition/meals/meal-public-id/"
            "foods/food-public-id/update"
        ),
        (
            "/api/agent/nutrition/meals/meal-public-id/"
            "foods/food-public-id/delete"
        ),
        "/api/agent/nutrition/preferences/facts",
    ]

    responses = [client.post(path, json={}) for path in paths]

    assert [response.status_code for response in responses] == [
        404,
        404,
        404,
        404,
        404,
        404,
    ]


def test_nutrition_ontology_seed_is_idempotent():
    with build_session() as session:
        first = seed_nutrition_ontology(session)
        second = seed_nutrition_ontology(session)
        session.commit()

        assert first["success"] is True
        assert second["nodes_created_or_updated"] == 0
        assert second["triples_created_or_updated"] == 0
        assert session.query(NutritionOntologyNode).count() >= 8
        assert session.query(NutritionOntologyTriple).count() >= 3
        assert session.query(NutritionOntologyTriple).filter(NutritionOntologyTriple.predicate == "contains_allergen").count() >= 1


def test_nutrition_preference_facts_are_upserted_and_summarized():
    with build_session() as session:
        first = record_preference_fact(
            session,
            patient_id="patient-pref",
            predicate="dislikes",
            object_label="짜장면",
            object_type="food",
            evidence_text="짜장면 싫어",
        )
        second = record_preference_fact(
            session,
            patient_id="patient-pref",
            predicate="dislikes",
            object_label="짜장면",
            object_type="food",
            confidence=0.8,
            evidence_text="짜장면은 별로야",
        )
        record_preference_fact(
            session,
            patient_id="patient-pref",
            predicate="allergic_to",
            object_label="땅콩",
            object_type="ingredient",
            evidence_text="땅콩 알레르기가 있어",
        )
        session.commit()

        summary = nutrition_preference_summary(session, patient_id="patient-pref")

        assert first["fact"]["id"] == second["fact"]["id"]
        assert session.query(NutritionPatientPreferenceTriple).count() == 2
        assert summary["counts"] == {"hard": 1, "soft": 1, "total": 2}
        assert summary["hard_constraints"][0]["object_label"] == "땅콩"
        assert summary["soft_preferences"][0]["object_label"] == "짜장면"


def test_preference_csv_lists_are_recorded_as_soft_facts():
    with build_session() as session:
        result = record_preference_csv_lists(
            session,
            patient_id="patient-csv-pref",
            preferred_foods_csv="현미밥, 연어",
            avoided_foods_csv="짜장면, 매운 음식",
            diet_styles_csv="저나트륨, 고단백",
        )
        session.commit()

        summary = nutrition_preference_summary(session, patient_id="patient-csv-pref")
        facts = {(item["predicate"], item["object_label"], item["object_type"]) for item in summary["soft_preferences"]}

        assert result["recorded_count"] == 6
        assert ("likes", "현미밥", "food") in facts
        assert ("likes", "연어", "food") in facts
        assert ("dislikes", "짜장면", "food") in facts
        assert ("dislikes", "매운 음식", "food") in facts
        assert ("prefers", "저나트륨", "diet_style") in facts
        assert ("prefers", "고단백", "diet_style") in facts


def test_preference_annotations_are_added_to_food_search_candidates():
    with build_session() as session:
        session.add(
            NutritionFoodRef(
                food_ref_id="test-jjajangmyeon",
                food_name="짜장면",
                category="면류",
                serving_size=100,
                energy=150,
                protein=5,
                sodium=300,
                fat=4,
                carbohydrate=25,
            )
        )
        record_preference_fact(
            session,
            patient_id="patient-search",
            predicate="dislikes",
            object_label="짜장면",
            object_type="food",
            evidence_text="짜장면 싫어",
        )
        session.commit()

        result = search_foods("짜장", session=session, patient_id="patient-search")
        candidate = result["candidates"][0]

        assert candidate["food_name"] == "짜장면"
        assert candidate["preference_match"]["status"] == "negative"
        assert candidate["soft_preference_notes"][0]["object_label"] == "짜장면"

        record_preference_fact(
            session,
            patient_id="patient-hard-search",
            predicate="medically_avoids",
            object_label="짜장면",
            object_type="food",
            evidence_text="의사가 짜장면은 피하라고 했어",
        )
        session.commit()

        blocked = search_foods("짜장", session=session, patient_id="patient-hard-search")["candidates"][0]

        assert blocked["preference_match"]["status"] == "blocked"
        assert blocked["hard_constraint_violations"][0]["object_label"] == "짜장면"


def test_allergy_relation_blocks_food_candidate_by_allergen_triple():
    with build_session() as session:
        session.add(
            NutritionFoodRef(
                food_ref_id="test-soybean-stew",
                food_name="두부된장국",
                category="국류",
                serving_size=100,
                energy=70,
                protein=6,
                sodium=250,
                fat=3,
                carbohydrate=5,
            )
        )
        seed_nutrition_ontology(session)
        record_preference_fact(
            session,
            patient_id="patient-soy-allergy",
            predicate="allergic_to",
            object_label="대두",
            object_type="ingredient",
            evidence_text="대두 알레르기가 있어",
        )
        session.commit()

        result = search_foods("두부", session=session, patient_id="patient-soy-allergy")
        candidate = result["candidates"][0]

        assert candidate["food_name"] == "두부된장국"
        assert candidate["preference_match"]["status"] == "blocked"
        assert candidate["hard_constraint_violations"][0]["object_label"] == "대두"
        assert {relation["object_label"] for relation in candidate["ontology_relations"]["contains_allergen"]} == {"대두"}


def test_nutrition_preference_service_write_and_db_read_are_available():
    client = TestClient(app)
    reset_testbed(client)

    with SessionLocal() as session:
        session.add(
            NutritionFoodRef(
                food_ref_id="food-api-pref-jjajang",
                food_name="짜장밥",
                category="밥류",
                serving_size=100,
                energy=180,
                carbohydrate=32,
                protein=5,
                fat=4,
                sodium=420,
                source="test",
            )
        )
        recorded = record_preference_fact(
            session,
            patient_id="patient-api-pref",
            predicate="allergic_to",
            object_label="땅콩",
            object_type="ingredient",
            evidence_text="땅콩 알레르기가 있어",
        )
        session.commit()
        summary = nutrition_preference_summary(
            session,
            patient_id="patient-api-pref",
        )
        search = search_foods(
            "짜장",
            session=session,
            patient_id="patient-api-pref",
        )
    assert recorded["fact"]["safety_level"] == "hard"
    assert summary["counts"]["hard"] == 1
    assert search["candidates"]
    assert "preference_match" in search["candidates"][0]
