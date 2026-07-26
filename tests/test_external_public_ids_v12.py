from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from agent_app.integration.chat_contracts import ChatSyncRequest
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
)
from shared.backend_v12_contracts import RecordChangeRequest
from shared.tool_catalog import ToolCatalog
from system_app.migrations import ensure_external_public_ids, run_migrations
from system_app.models import (
    Base,
    ChatMessage,
    DoseEvent,
    NutritionFood,
    NutritionMeal,
)
from system_app.services.backend_v12_service import apply_record_change

NOW = datetime(2026, 7, 25, 9, 30, tzinfo=UTC)


def test_external_public_id_migration_backfills_without_replacing_internal_pk() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE chat_messages "
                "(id INTEGER PRIMARY KEY, role VARCHAR(20), public_id VARCHAR(80))"
            )
        )
        connection.execute(
            text("CREATE TABLE dose_events (id INTEGER PRIMARY KEY)")
        )
        connection.execute(
            text(
                "CREATE TABLE nutrition_meals "
                "(id INTEGER PRIMARY KEY, public_id VARCHAR(80))"
            )
        )
        connection.execute(
            text("CREATE TABLE nutrition_foods (id INTEGER PRIMARY KEY)")
        )
        connection.execute(
            text(
                "INSERT INTO chat_messages (id, role, public_id) VALUES "
                "(10, 'user', NULL), (20, 'assistant', NULL)"
            )
        )
        connection.execute(text("INSERT INTO dose_events (id) VALUES (120)"))
        connection.execute(
            text(
                "INSERT INTO nutrition_meals (id, public_id) "
                "VALUES (42, 'meal_preserved')"
            )
        )
        connection.execute(text("INSERT INTO nutrition_foods (id) VALUES (51)"))

    ensure_external_public_ids(engine)
    ensure_external_public_ids(engine)

    with engine.connect() as connection:
        messages = connection.execute(
            text("SELECT id, public_id FROM chat_messages ORDER BY id")
        ).all()
        dose = connection.execute(
            text("SELECT id, public_id FROM dose_events")
        ).one()
        meal = connection.execute(
            text("SELECT id, public_id FROM nutrition_meals")
        ).one()
        food = connection.execute(
            text("SELECT id, public_id FROM nutrition_foods")
        ).one()

    assert messages[0][0] == 10
    assert str(messages[0][1]).startswith("user_msg_")
    assert messages[1][0] == 20
    assert str(messages[1][1]).startswith("assistant_msg_")
    assert dose[0] == 120 and str(dose[1]).startswith("dose_")
    assert meal == (42, "meal_preserved")
    assert food[0] == 51 and str(food[1]).startswith("food_")
    engine.dispose()


def test_backend_record_change_accepts_public_and_legacy_ids_but_returns_public_ids(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "public-write.db").as_posix()
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, future=True)

    with sessions() as session:
        confirmation = ChatMessage(
            public_id="user_msg_100",
            patient_id="patient-public",
            conversation_id="conversation-public",
            ai_request_id="chat-request-public",
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="변경해줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        dose = DoseEvent(
            public_id="dose_120",
            patient_id="patient-public",
            plan_id=1,
            schedule_id=1,
            medication_name="테스트약",
            slot_label="아침",
            scheduled_for=NOW.replace(tzinfo=None),
            status="scheduled",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        meal = NutritionMeal(
            public_id="meal_42",
            patient_id="patient-public",
            meal_type="breakfast",
            meal_date=NOW.date(),
            meal_time="09:00",
            description="변경 전",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add_all([confirmation, dose, meal])
        session.flush()
        food = NutritionFood(
            public_id="food_51",
            meal_id=meal.id,
            food_name="사과",
            portion="1개",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add(food)
        session.commit()
        meal_internal_id = meal.id

        dose_response = apply_record_change(
            session,
            _record_request(
                request_id="write-dose-public",
                resource_type="medication_dose_event",
                operation="update",
                record_id="dose_120",
                expected_version=1,
                payload={"status": "taken", "taken_at": NOW.isoformat()},
            ),
        )
        meal_response = apply_record_change(
            session,
            _record_request(
                request_id="write-meal-legacy",
                resource_type="nutrition_meal",
                operation="update",
                record_id=str(meal_internal_id),
                expected_version=1,
                payload={"description": "변경 후"},
            ),
        )
        food_response = apply_record_change(
            session,
            _record_request(
                request_id="write-food-public",
                resource_type="nutrition_food",
                operation="update",
                record_id="food_51",
                parent_record_id="meal_42",
                expected_version=1,
                payload={"food_name": "배"},
            ),
        )
        invalid_response = apply_record_change(
            session,
            _record_request(
                request_id="write-invalid-public",
                resource_type="nutrition_meal",
                operation="update",
                record_id="meal_missing",
                expected_version=1,
                payload={"description": "무효"},
            ),
        )
        create_response = apply_record_change(
            session,
            RecordChangeRequest.model_validate(
                {
                    "request_id": "write-meal-create-public",
                    "source_chat_request_id": "chat-request-public",
                    "conversation_id": "conversation-public",
                    "confirmation_message_id": "user_msg_100",
                    "patient_id": "patient-public",
                    "resource_type": "nutrition_meal",
                    "operation": "create",
                    "record_id": None,
                    "parent_record_id": None,
                    "expected_version": None,
                    "payload": {
                        "meal_type": "lunch",
                        "meal_date": NOW.date().isoformat(),
                        "foods": [{"food_name": "두부", "nutrients": {}}],
                    },
                    "requested_at": NOW.isoformat(),
                }
            ),
        )

    assert dose_response.result is not None
    assert dose_response.result.record_id == "dose_120"
    assert meal_response.result is not None
    assert meal_response.result.record_id == "meal_42"
    assert food_response.result is not None
    assert food_response.result.record_id == "food_51"
    assert food_response.result.parent_record_id == "meal_42"
    assert invalid_response.success is False
    assert invalid_response.error is not None
    assert invalid_response.error.code == "nutrition_meal_not_found"
    assert create_response.result is not None
    assert create_response.result.record_id.startswith("meal_")
    assert not create_response.result.record_id.isdigit()
    engine.dispose()


def test_ai_read_boundary_resolves_legacy_inputs_and_never_returns_entity_pks(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "public-read.db").as_posix()
    engine = create_engine(f"sqlite:///{database_path}", future=True)
    Base.metadata.create_all(engine)
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, future=True)
    with sessions() as session:
        user = ChatMessage(
            public_id="user_msg_100",
            patient_id="patient-public",
            conversation_id="conversation-public",
            ai_request_id="chat-request-public",
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="조회해줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        assistant = ChatMessage(
            public_id="assistant_msg_200",
            patient_id="patient-public",
            conversation_id="conversation-public",
            ai_request_id="chat-request-public",
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type="text",
            content="확인했어요.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        dose = DoseEvent(
            public_id="dose_120",
            patient_id="patient-public",
            plan_id=1,
            schedule_id=1,
            medication_name="테스트약",
            slot_label="아침",
            scheduled_for=NOW.replace(tzinfo=None),
            status="scheduled",
            version=4,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        meal = NutritionMeal(
            public_id="meal_42",
            patient_id="patient-public",
            meal_type="breakfast",
            meal_date=NOW.date(),
            meal_time="09:00",
            version=2,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add_all([user, assistant, dose, meal])
        session.flush()
        assistant.reply_to_message_id = user.id
        food = NutritionFood(
            public_id="food_51",
            meal_id=meal.id,
            food_name="사과",
            portion="1개",
            version=3,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add(food)
        session.commit()
        user_internal_id = user.id
        assistant_internal_id = assistant.id
        dose_internal_id = dose.id
        meal_internal_id = meal.id
        food_internal_id = food.id

    queries = BackendQueryTools(f"sqlite:///{database_path}")
    chat = queries.validate_chat_message(
        ChatSyncRequest(
            request_id="chat-request-public",
            message_id=str(user_internal_id),
            conversation_id="conversation-public",
            patient_id="patient-public",
            requested_return_type=None,
            message="조회해줘.",
            message_at=NOW,
        )
    )
    feedback = queries.validate_feedback_target(
        message_id=str(assistant_internal_id),
        conversation_id="conversation-public",
        patient_id="patient-public",
    )
    dose_status = queries.medication_dose_status(
        patient_id="patient-public",
        target_date=NOW.date().isoformat(),
    )
    meals = queries.nutrition_meals(
        patient_id="patient-public",
        meal_date=NOW.date().isoformat(),
    )

    assert chat["recent_chat"][0]["message_id"] == "user_msg_100"
    assert feedback == {
        "message_id": "assistant_msg_200",
        "conversation_id": "conversation-public",
        "patient_id": "patient-public",
        "role": "assistant",
    }
    assert dose_status["dose_events"][0]["dose_event_id"] == "dose_120"
    assert meals["meals"][0]["id"] == "meal_42"
    assert meals["meals"][0]["foods"][0]["id"] == "food_51"
    assert queries.record_version(
        patient_id="patient-public",
        resource_type="medication_dose_event",
        record_id=str(dose_internal_id),
    ) == 4
    assert queries.record_version(
        patient_id="patient-public",
        resource_type="nutrition_meal",
        record_id="meal_42",
    ) == 2
    assert queries.record_version(
        patient_id="patient-public",
        resource_type="nutrition_food",
        record_id=str(food_internal_id),
        parent_record_id=str(meal_internal_id),
    ) == 3
    with pytest.raises(BackendChatMessageNotFound):
        queries.validate_feedback_target(
            message_id="user_msg_100",
            conversation_id="conversation-public",
            patient_id="patient-public",
        )
    queries.engine.dispose()
    engine.dispose()


def test_model_visible_v12_entity_identifiers_are_opaque_strings() -> None:
    tools = {tool["name"]: tool for tool in ToolCatalog.available_tools_payload()}
    expected_fields = {
        "update_medication_dose_event_status": ("dose_event_id",),
        "update_nutrition_meal_record": ("meal_id",),
        "delete_nutrition_meal_record": ("meal_id",),
        "update_nutrition_food_record": ("meal_id", "food_id"),
        "delete_nutrition_food_record": ("meal_id", "food_id"),
    }
    for tool_name, fields in expected_fields.items():
        schema = tools[tool_name]["inputSchema"]
        assert schema["additionalProperties"] is False
        for field_name in fields:
            assert schema["properties"][field_name]["type"] == "string"


def _record_request(
    *,
    request_id: str,
    resource_type: str,
    operation: str,
    record_id: str,
    expected_version: int,
    payload: dict,
    parent_record_id: str | None = None,
) -> RecordChangeRequest:
    return RecordChangeRequest.model_validate(
        {
            "request_id": request_id,
            "source_chat_request_id": "chat-request-public",
            "conversation_id": "conversation-public",
            "confirmation_message_id": "user_msg_100",
            "patient_id": "patient-public",
            "resource_type": resource_type,
            "operation": operation,
            "record_id": record_id,
            "parent_record_id": parent_record_id,
            "expected_version": expected_version,
            "payload": payload,
            "requested_at": NOW.isoformat(),
        }
    )
