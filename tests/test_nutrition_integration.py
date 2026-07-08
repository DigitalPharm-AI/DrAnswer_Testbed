import json

from fastapi.testclient import TestClient

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
from system_app.services.medication_plan_service import reset_simulation_state
from system_app.services.food_search_service import english_to_korean_nutrients, scale_nutrients
from system_app.services.diet_recommendation_service import recommend_diet
from system_app.services.nutrition_service import daily_nutrition_view, record_meal, run_nutrition_scenario, search_foods
from system_app.services.nutrition_preference_service import (
    nutrition_preference_summary,
    parse_preference_csv,
    record_preference_fact,
    record_preference_csv_lists,
    seed_nutrition_ontology,
)
from system_app.services.system_request_service import build_multiturn_chat_request, create_system_event_request
from tests.helpers import build_session


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


def test_nutrition_panel_and_scenario_route_render():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "영양 관리" in response.text
    assert "식단 선호도 입력" in response.text
    assert 'name="preferred_foods_csv"' in response.text
    assert 'name="avoided_foods_csv"' in response.text
    assert 'name="diet_styles_csv"' in response.text
    assert "/nutrition/preferences" in response.text
    assert "정상 아침" in response.text
    assert "점심 기록 + 영양 알림" in response.text
    assert "nutrition-progress" in response.text
    assert "/nutrition/scenarios/high_sodium_lunch" in response.text
    assert 'id="overview-summary"' not in response.text
    assert 'class="overview-card' not in response.text
    assert ">오늘 복약</span>" not in response.text
    assert ">오늘 영양</span>" not in response.text
    assert ">활성 알림</span>" not in response.text
    assert response.text.index('class="dose-calendar"') < response.text.index('id="nutrition-panel"')
    assert response.text.index('id="active-policies-panel"') < response.text.index('id="notifications-panel"')

    overview_response = client.get("/partials/overview-summary")
    assert overview_response.status_code == 404

    scenario_response = client.post("/nutrition/scenarios/high_sodium_lunch")

    assert scenario_response.status_code == 200
    assert "nutrition-panel" in scenario_response.text
    assert "짜장면, 탕수육" in scenario_response.text
    assert "기록됨" in scenario_response.text


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


def test_diet_recommendation_uses_serving_size_nutrients_and_reasons():
    with build_session() as session:
        session.add_all(
            [
                NutritionFoodRef(
                    food_ref_id="low-sodium-serving",
                    food_name="저나트륨 기준량 음식",
                    category="테스트",
                    serving_size=200,
                    energy=150,
                    protein=4,
                    sodium=100,
                    fat=2,
                    carbohydrate=20,
                ),
                NutritionFoodRef(
                    food_ref_id="high-serving-sodium",
                    food_name="제공량 초과 나트륨 음식",
                    category="테스트",
                    serving_size=400,
                    energy=100,
                    protein=3,
                    sodium=200,
                    fat=1,
                    carbohydrate=18,
                ),
            ]
        )
        session.commit()

        result = recommend_diet(session, patient_id="patient-serving-recommend", constraints={"나트륨": "low"}, limit=5)
        names = {item["food_name"] for item in result["recommendations"]}
        recommendation = next(item for item in result["recommendations"] if item["food_name"] == "저나트륨 기준량 음식")

        assert "저나트륨 기준량 음식" in names
        assert "제공량 초과 나트륨 음식" not in names
        assert recommendation["nutrients"]["sodium"]["value"] == 200
        assert recommendation["recommendation_reasons"]
        assert "나트륨" in recommendation["recommendation_reasons"][0]


def test_diet_recommendation_randomizes_candidate_query_by_default(monkeypatch):
    with build_session() as session:
        session.add_all(
            [
                NutritionFoodRef(
                    food_ref_id=f"random-low-sodium-{index}",
                    food_name=f"랜덤 저나트륨 음식 {index}",
                    category="테스트",
                    serving_size=100,
                    energy=120 + index,
                    protein=5,
                    sodium=100,
                    fat=2,
                    carbohydrate=20,
                )
                for index in range(6)
            ]
        )
        session.commit()

        captured_statements: list[str] = []
        original_scalars = session.scalars

        def capture_scalars(statement, *args, **kwargs):
            statement_text = str(statement)
            if "nutrition_food_ref" in statement_text:
                captured_statements.append(statement_text)
            return original_scalars(statement, *args, **kwargs)

        monkeypatch.setattr(session, "scalars", capture_scalars)

        result = recommend_diet(session, patient_id="patient-random-recommend", constraints={"나트륨": "low"}, limit=3)

        assert result["randomized"] is True
        assert len(result["recommendations"]) == 3
        assert any("order by random()" in statement.lower() for statement in captured_statements)


def test_nutrition_scenario_route_returns_public_error_code():
    client = TestClient(app)
    raw_scenario_key = "pytest-private-peanut-allergy-scenario"

    response = client.post(f"/nutrition/scenarios/{raw_scenario_key}")

    assert response.status_code == 400
    assert response.json()["detail"] == "unknown_nutrition_scenario"
    assert raw_scenario_key not in response.text


def test_preference_csv_parser_accepts_comma_lists_without_brackets():
    assert parse_preference_csv("현미밥, 연어, 두부") == ["현미밥", "연어", "두부"]
    assert parse_preference_csv("현미밥,, 연어, 현미밥") == ["현미밥", "연어"]
    assert parse_preference_csv("(짜장면), [매운 음식]") == ["짜장면", "매운 음식"]


def test_agent_nutrition_api_is_available():
    client = TestClient(app)

    search_response = client.post("/api/agent/nutrition/food/search", json={"query": "짜장"})

    assert search_response.status_code == 200
    assert search_response.json()["candidates"][0]["food_name"] == "짜장면"


def test_agent_nutrition_api_keeps_meal_queries_patient_scoped():
    client = TestClient(app)
    client.post("/simulation/reset")
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

    response_a = client.post(
        "/api/agent/nutrition/meals",
        json={**meal_payload, "patient_id": "patient-a", "meal_type": "breakfast"},
    )
    response_b = client.post(
        "/api/agent/nutrition/meals",
        json={**meal_payload, "patient_id": "patient-b", "meal_type": "lunch"},
    )
    list_a = client.get("/api/agent/nutrition/meals", params={"patient_id": "patient-a", "meal_date": "2026-04-20"})
    list_b = client.get("/api/agent/nutrition/meals", params={"patient_id": "patient-b", "meal_date": "2026-04-20"})
    summary_b = client.get("/api/agent/nutrition/daily-summary", params={"patient_id": "patient-b", "meal_date": "2026-04-20"})

    assert response_a.status_code == 200
    assert response_b.status_code == 200
    assert list_a.status_code == 200
    assert list_b.status_code == 200
    assert summary_b.status_code == 200
    assert list_a.json()["total"] == 1
    assert list_b.json()["total"] == 1
    assert {meal["patient_id"] for meal in list_a.json()["meals"]} == {"patient-a"}
    assert {meal["patient_id"] for meal in list_b.json()["meals"]} == {"patient-b"}
    assert summary_b.json()["daily_summary"]["patient_id"] == "patient-b"
    assert summary_b.json()["daily_summary"]["total_meals"] == 1


def test_agent_nutrition_api_updates_and_deletes_meal_records():
    client = TestClient(app)
    client.post("/simulation/reset")
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

    create_response = client.post("/api/agent/nutrition/meals", json=meal_payload)
    meal_id = create_response.json()["meal"]["id"]
    update_response = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/update",
        json={
            "patient_id": "patient-crud",
            "meal_type": "dinner",
            "description": "corrected meal",
            "foods": [
                {
                    "food_name": "crud updated meal",
                    "portion": "2 pieces",
                    "nutrients": {"calories": 180, "protein": 10, "sodium": 150, "fat": 4, "carbohydrates": 20},
                }
            ],
            "reason": "patient correction",
        },
    )
    wrong_patient_delete = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/delete",
        json={"patient_id": "other-patient", "reason": "wrong patient should not delete"},
    )
    delete_response = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/delete",
        json={"patient_id": "patient-crud", "reason": "patient requested delete"},
    )
    list_response = client.get("/api/agent/nutrition/meals", params={"patient_id": "patient-crud", "meal_date": "2026-04-20"})

    assert create_response.status_code == 200
    assert update_response.status_code == 200
    updated = update_response.json()["meal"]
    assert updated["id"] == meal_id
    assert updated["meal_type"] == "dinner"
    assert updated["description"] == "corrected meal"
    assert updated["foods"][0]["food_name"] == "crud updated meal"
    assert wrong_patient_delete.status_code == 404
    assert wrong_patient_delete.json()["detail"] == "nutrition_meal_not_found"
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_meal"]["id"] == meal_id
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 0


def test_agent_nutrition_api_updates_and_deletes_food_records():
    client = TestClient(app)
    client.post("/simulation/reset")
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

    create_response = client.post("/api/agent/nutrition/meals", json=meal_payload)
    meal = create_response.json()["meal"]
    meal_id = meal["id"]
    first_food_id = meal["foods"][0]["id"]
    second_food_id = meal["foods"][1]["id"]

    update_response = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/foods/{first_food_id}/update",
        json={
            "patient_id": "patient-food-crud",
            "food_ref_id": "guobaorou-ref",
            "food_name": "꿔바로우",
            "portion": "1 plate",
            "nutrients": {"calories": 450, "protein": 16, "sodium": 550, "fat": 22, "carbohydrates": 44},
            "reason": "patient corrected food",
        },
    )
    wrong_patient_delete = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/foods/{second_food_id}/delete",
        json={"patient_id": "other-patient", "reason": "wrong patient should not delete"},
    )
    delete_response = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/foods/{second_food_id}/delete",
        json={"patient_id": "patient-food-crud", "reason": "patient removed rice"},
    )
    delete_last_response = client.post(
        f"/api/agent/nutrition/meals/{meal_id}/foods/{first_food_id}/delete",
        json={"patient_id": "patient-food-crud", "reason": "patient removed last food"},
    )
    list_response = client.get("/api/agent/nutrition/meals", params={"patient_id": "patient-food-crud", "meal_date": "2026-04-20"})

    assert create_response.status_code == 200
    assert update_response.status_code == 200
    updated_payload = update_response.json()
    assert updated_payload["food"]["id"] == first_food_id
    assert updated_payload["food"]["food_name"] == "꿔바로우"
    assert updated_payload["food"]["food_ref_id"] == "guobaorou-ref"
    assert updated_payload["meal"]["foods"][0]["food_name"] == "꿔바로우"
    assert wrong_patient_delete.status_code == 404
    assert wrong_patient_delete.json()["detail"] == "nutrition_meal_not_found"
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted_food"]["id"] == second_food_id
    assert delete_response.json()["meal_deleted"] is False
    assert len(delete_response.json()["meal"]["foods"]) == 1
    assert delete_last_response.status_code == 200
    assert delete_last_response.json()["meal_deleted"] is True
    assert delete_last_response.json()["meal"] is None
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 0


def test_nutrition_panel_deletes_single_food_and_rerenders_partial():
    client = TestClient(app)
    client.post("/simulation/reset")
    meal_payload = {
        "patient_id": "patient-panel-food-delete",
        "meal_type": "lunch",
        "meal_date": "2026-04-20",
        "meal_time": "12:00:00",
        "foods": [
            {
                "food_name": "삭제 대상 음식",
                "portion": "1 piece",
                "nutrients": {"calories": 120, "protein": 3, "sodium": 20, "fat": 2, "carbohydrates": 24},
            }
        ],
    }
    create_response = client.post("/api/agent/nutrition/meals", json=meal_payload)
    meal = create_response.json()["meal"]

    response = client.post(
        f"/nutrition/meals/{meal['id']}/foods/{meal['foods'][0]['id']}/delete",
        data={"patient_id": "patient-panel-food-delete"},
    )
    list_response = client.get("/api/agent/nutrition/meals", params={"patient_id": "patient-panel-food-delete", "meal_date": "2026-04-20"})

    assert response.status_code == 200
    assert 'id="nutrition-panel"' in response.text
    assert list_response.status_code == 200
    assert list_response.json()["total"] == 0


def test_agent_nutrition_record_meal_returns_public_error_for_invalid_nutrients():
    client = TestClient(app)
    raw_value = "pytest-private-peanut-allergy-nutrient"

    response = client.post(
        "/api/agent/nutrition/meals",
        json={
            "patient_id": "patient-invalid-nutrients",
            "meal_type": "lunch",
            "foods": [
                {
                    "food_name": "검증 식사",
                    "nutrients": {
                        "calories": raw_value,
                        "protein": 4,
                        "sodium": 100,
                        "fat": 2,
                        "carbohydrates": 12,
                    },
                }
            ],
        },
    )
    negative_response = client.post(
        "/api/agent/nutrition/meals",
        json={
            "patient_id": "patient-negative-nutrients",
            "meal_type": "lunch",
            "foods": [
                {
                    "food_name": "음수 식사",
                    "nutrients": {
                        "calories": -1,
                    },
                }
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid_nutrient_value"
    assert raw_value not in response.text
    assert negative_response.status_code == 422
    assert negative_response.json()["detail"] == "negative_nutrient_value"


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


def test_nutrition_preference_form_route_records_comma_lists():
    client = TestClient(app)

    response = client.post(
        "/nutrition/preferences",
        data={
            "patient_id": "patient-ui-csv-pref",
            "preferred_foods_csv": "현미밥, 연어",
            "avoided_foods_csv": "짜장면, 매운 음식",
            "diet_styles_csv": "저나트륨, 고단백",
        },
    )
    summary_response = client.get("/api/agent/nutrition/preferences", params={"patient_id": "patient-ui-csv-pref"})
    preferences = summary_response.json()["preferences"]
    facts = {(item["predicate"], item["object_label"], item["object_type"]) for item in preferences["soft_preferences"]}

    assert response.status_code == 204
    assert response.headers["HX-Refresh"] == "true"
    assert summary_response.status_code == 200
    assert ("likes", "현미밥", "food") in facts
    assert ("likes", "연어", "food") in facts
    assert ("dislikes", "짜장면", "food") in facts
    assert ("dislikes", "매운 음식", "food") in facts
    assert ("prefers", "저나트륨", "diet_style") in facts
    assert ("prefers", "고단백", "diet_style") in facts


def test_preference_annotations_are_added_to_food_search_candidates():
    with build_session() as session:
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


def test_agent_nutrition_preference_api_and_context_are_available():
    client = TestClient(app)
    client.post("/simulation/reset")

    response = client.post(
        "/api/agent/nutrition/preferences/facts",
        json={
            "patient_id": "patient-api-pref",
            "predicate": "allergic_to",
            "object_label": "땅콩",
            "object_type": "ingredient",
            "evidence_text": "땅콩 알레르기가 있어",
        },
    )
    summary_response = client.get("/api/agent/nutrition/preferences", params={"patient_id": "patient-api-pref"})
    search_response = client.post("/api/agent/nutrition/food/search", json={"query": "짜장", "patient_id": "patient-api-pref"})

    assert response.status_code == 200
    assert response.json()["fact"]["safety_level"] == "hard"
    assert summary_response.status_code == 200
    assert summary_response.json()["preferences"]["counts"]["hard"] == 1
    assert search_response.status_code == 200
    assert "preference_match" in search_response.json()["candidates"][0]


def test_multiturn_chat_request_includes_nutrition_context():
    with build_session() as session:
        record_preference_fact(
            session,
            predicate="prefers",
            object_label="한식",
            object_type="cuisine",
            evidence_text="한식 위주가 좋아",
        )
        run_nutrition_scenario(session, "normal_breakfast")
        request_notification = create_system_event_request(session, "multiturn_chat", "오늘 나트륨 얼마나 남았어?")
        session.commit()

        request = build_multiturn_chat_request(session, "multiturn_chat", "오늘 나트륨 얼마나 남았어?", request_notification.id)

        assert "nutrition" in request.context
        assert request.context["nutrition"]["today_summary"]["total_meals"] == 1
        assert request.context["nutrition"]["today_meals"][0]["meal_label"] == "아침"
        assert request.context["nutrition"]["preferences"]["soft_preferences"][0]["object_label"] == "한식"


def test_multiturn_chat_request_uses_persisted_unique_conversation_id():
    with build_session() as session:
        first = create_system_event_request(session, "multiturn_chat", "속이 메스꺼운데 약 때문일까요?")
        second = create_system_event_request(session, "multiturn_chat", "오늘 점심 조정해줘")
        session.commit()

        first_metadata = json.loads(first.metadata_json)
        second_metadata = json.loads(second.metadata_json)
        first_request = build_multiturn_chat_request(session, "multiturn_chat", first_metadata["request_message"], first.id)
        second_request = build_multiturn_chat_request(session, "multiturn_chat", second_metadata["request_message"], second.id)

        assert first_metadata["agent_conversation_id"].startswith("system-event-")
        assert second_metadata["agent_conversation_id"].startswith("system-event-")
        assert first_metadata["agent_conversation_id"] != f"system-event-{first.id}"
        assert first_metadata["agent_conversation_id"] != second_metadata["agent_conversation_id"]
        assert first_request.callback_context.conversation_id == first_metadata["agent_conversation_id"]
        assert second_request.callback_context.conversation_id == second_metadata["agent_conversation_id"]
