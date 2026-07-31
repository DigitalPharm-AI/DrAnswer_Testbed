from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

from shared.chat_contracts import ChatSyncRequest
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
    BackendRecordNotFound,
)
from shared.backend_v13_contracts import (
    CommonErrorResponse,
    RecordChangeRequest,
)
from shared.tool_catalog import ToolCatalog
from shared.tool_names import (
    CREATE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
    UPDATE_NUTRITION_FOOD_RECORD,
    UPDATE_NUTRITION_MEAL_RECORD,
)
from system_app.migrations import ensure_external_public_ids, run_migrations
from system_app.models import (
    Base,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    NutritionFood,
    NutritionMeal,
)
from system_app.services.backend_v13_service import apply_record_change
from tests.helpers import (
    build_backend_reader_url,
    build_system_engine,
)

NOW = datetime(2026, 7, 25, 9, 30, tzinfo=UTC)
PATIENT_ID = "patient_000000001a2b3c4d"
CONTEXT_PATIENT_ID = "patient_000000002b3c4d5e"
CHAT_REQUEST_ID = "req_000000001a2b3c4d"
CONFIRMATION_MESSAGE_ID = "user_msg_000000001a2b3c4d"
ASSISTANT_MESSAGE_ID = "assistant_msg_000000001a2b3c4d"
DOSE_EVENT_ID = "dose_000000001a2b3c4d"
MEAL_ID = "meal_000000001a2b3c4d"
FOOD_ID = "food_000000001a2b3c4d"

WRITE_DOSE_REQUEST_ID = "req_00000000a0000001"
WRITE_MEAL_REQUEST_ID = "req_00000000a0000002"
WRITE_NUMERIC_MEAL_REQUEST_ID = "req_00000000a0000003"
WRITE_FOOD_REQUEST_ID = "req_00000000a0000004"
WRITE_MISSING_MEAL_REQUEST_ID = "req_00000000a0000005"
WRITE_CREATE_MEAL_REQUEST_ID = "req_00000000a0000006"

ORIGIN_REQUEST_ID = "req_00000000b0000001"
CHOICE_REQUEST_ID = "req_00000000b0000002"
ORIGIN_MESSAGE_ID = "user_msg_00000000b0000001"
CARD_MESSAGE_ID = "assistant_msg_00000000b0000001"
CHOICE_MESSAGE_ID = "user_msg_00000000b0000002"


def test_external_public_id_migration_backfills_without_replacing_internal_pk() -> None:
    engine, cleanup = build_system_engine(
        "external_public_id_migration",
        create_models=False,
    )
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
                "CREATE TABLE notifications "
                "(id INTEGER PRIMARY KEY, public_id VARCHAR(80))"
            )
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
                "VALUES (42, 'meal_00000000deadbeef')"
            )
        )
        connection.execute(text("INSERT INTO nutrition_foods (id) VALUES (51)"))
        connection.execute(text("INSERT INTO notifications (id) VALUES (61)"))

    Base.metadata.create_all(bind=engine)
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
        notification = connection.execute(
            text("SELECT id, public_id FROM notifications")
        ).one()

    assert messages[0][0] == 10
    assert re.fullmatch(r"user_msg_[0-9a-f]{16}", str(messages[0][1]))
    assert messages[1][0] == 20
    assert re.fullmatch(
        r"assistant_msg_[0-9a-f]{16}",
        str(messages[1][1]),
    )
    assert dose[0] == 120
    assert re.fullmatch(r"dose_[0-9a-f]{16}", str(dose[1]))
    assert meal == (42, "meal_00000000deadbeef")
    assert food[0] == 51
    assert re.fullmatch(r"food_[0-9a-f]{16}", str(food[1]))
    assert notification[0] == 61
    assert str(notification[1]).startswith("notif_")
    notification_indexes = {
        index["name"]
        for index in inspect(engine).get_indexes("notifications")
    }
    assert "uq_notifications_public_id" in notification_indexes
    cleanup()


def test_backend_record_change_requires_public_ids_and_returns_public_ids(
    tmp_path: Path,
) -> None:
    engine, cleanup = build_system_engine("external_public_id_write")
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, future=True)

    with sessions() as session:
        plan = MedicationPlan(
            patient_id=PATIENT_ID,
            medication_name="?뚯뒪?몄빟",
            start_date=NOW.date(),
            end_date=NOW.date(),
            active=True,
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(
            plan_id=plan.id,
            slot_label="?꾩묠",
            scheduled_time="09:30",
        )
        session.add(schedule)
        session.flush()
        confirmation = ChatMessage(
            public_id=CONFIRMATION_MESSAGE_ID,
            patient_id=PATIENT_ID,
            ai_request_id=CHAT_REQUEST_ID,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="변경해줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        dose = DoseEvent(
            public_id=DOSE_EVENT_ID,
            patient_id=PATIENT_ID,
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name="테스트약",
            slot_label="아침",
            scheduled_for=NOW.replace(tzinfo=None),
            status="scheduled",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        meal = NutritionMeal(
            public_id=MEAL_ID,
            patient_id=PATIENT_ID,
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
            public_id=FOOD_ID,
            meal_id=meal.id,
            food_name="사과",
            portion="1개",
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add(food)
        session.commit()

        _authorize_record_approval(
            session,
            confirmation,
            write_request_id=WRITE_DOSE_REQUEST_ID,
            action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            model_arguments={"dose_event_id": DOSE_EVENT_ID},
            expected_version=1,
        )
        dose_response = apply_record_change(
            session,
            _record_request(
                request_id=WRITE_DOSE_REQUEST_ID,
                resource_type="medication_dose_event",
                operation="update",
                record_id=DOSE_EVENT_ID,
                expected_version=1,
                payload={"status": "taken", "taken_at": NOW.isoformat()},
            ),
        )
        _authorize_record_approval(
            session,
            confirmation,
            write_request_id=WRITE_MEAL_REQUEST_ID,
            action_name=UPDATE_NUTRITION_MEAL_RECORD,
            model_arguments={"meal_id": MEAL_ID},
            expected_version=1,
        )
        meal_response = apply_record_change(
            session,
            _record_request(
                request_id=WRITE_MEAL_REQUEST_ID,
                resource_type="nutrition_meal",
                operation="update",
                record_id=MEAL_ID,
                expected_version=1,
                payload={"description": "변경 후"},
            ),
        )
        with pytest.raises(ValidationError, match="invalid_meal_id"):
            _record_request(
                request_id=WRITE_NUMERIC_MEAL_REQUEST_ID,
                resource_type="nutrition_meal",
                operation="update",
                record_id=str(meal.id),
                expected_version=2,
                payload={"description": "숫자 PK 입력"},
            )
        _authorize_record_approval(
            session,
            confirmation,
            write_request_id=WRITE_FOOD_REQUEST_ID,
            action_name=UPDATE_NUTRITION_FOOD_RECORD,
            model_arguments={"meal_id": MEAL_ID, "food_id": FOOD_ID},
            expected_version=1,
        )
        food_response = apply_record_change(
            session,
            _record_request(
                request_id=WRITE_FOOD_REQUEST_ID,
                resource_type="nutrition_food",
                operation="update",
                record_id=FOOD_ID,
                parent_record_id=MEAL_ID,
                expected_version=1,
                payload={"food_name": "배"},
            ),
        )
        _authorize_record_approval(
            session,
            confirmation,
            write_request_id=WRITE_MISSING_MEAL_REQUEST_ID,
            action_name=UPDATE_NUTRITION_MEAL_RECORD,
            model_arguments={"meal_id": "meal_00000000deadbeef"},
            expected_version=1,
        )
        invalid_response = apply_record_change(
            session,
            _record_request(
                request_id=WRITE_MISSING_MEAL_REQUEST_ID,
                resource_type="nutrition_meal",
                operation="update",
                record_id="meal_00000000deadbeef",
                expected_version=1,
                payload={"description": "무효"},
            ),
        )
        _authorize_record_approval(
            session,
            confirmation,
            write_request_id=WRITE_CREATE_MEAL_REQUEST_ID,
            action_name=CREATE_NUTRITION_MEAL_RECORD,
            model_arguments={
                "meal_type": "lunch",
                "meal_date": NOW.date().isoformat(),
                "foods": [{"food_name": "두부", "nutrients": {}}],
            },
            expected_version=None,
        )
        create_response = apply_record_change(
            session,
            RecordChangeRequest.model_validate(
                {
                    "request_id": WRITE_CREATE_MEAL_REQUEST_ID,
                    "source_chat_request_id": CHAT_REQUEST_ID,
                    "confirmation_message_id": CONFIRMATION_MESSAGE_ID,
                    "patient_id": PATIENT_ID,
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
    assert dose_response.result.record_id == DOSE_EVENT_ID
    assert meal_response.result is not None
    assert meal_response.result.record_id == MEAL_ID
    assert food_response.result is not None
    assert food_response.result.record_id == FOOD_ID
    assert food_response.result.parent_record_id == MEAL_ID
    assert isinstance(invalid_response, CommonErrorResponse)
    assert invalid_response.error.code == "NUTRITION_MEAL_NOT_FOUND"
    assert create_response.result is not None
    assert re.fullmatch(
        r"meal_[0-9a-f]{16}",
        create_response.result.record_id,
    )
    assert not create_response.result.record_id.isdigit()
    cleanup()


def test_ai_read_boundary_requires_public_inputs_and_never_returns_entity_pks(
    tmp_path: Path,
) -> None:
    engine, cleanup = build_system_engine("external_public_id_read")
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, future=True)
    with sessions() as session:
        plan = MedicationPlan(
            patient_id=PATIENT_ID,
            medication_name="?뚯뒪?몄빟",
            start_date=NOW.date(),
            end_date=NOW.date(),
            active=True,
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(
            plan_id=plan.id,
            slot_label="?꾩묠",
            scheduled_time="09:30",
        )
        session.add(schedule)
        session.flush()
        user = ChatMessage(
            public_id=CONFIRMATION_MESSAGE_ID,
            patient_id=PATIENT_ID,
            ai_request_id=CHAT_REQUEST_ID,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="조회해줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        assistant = ChatMessage(
            public_id=ASSISTANT_MESSAGE_ID,
            patient_id=PATIENT_ID,
            ai_request_id=CHAT_REQUEST_ID,
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type="text",
            content="확인했어요.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        dose = DoseEvent(
            public_id=DOSE_EVENT_ID,
            patient_id=PATIENT_ID,
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name="테스트약",
            slot_label="아침",
            scheduled_for=NOW.replace(tzinfo=None),
            status="scheduled",
            version=4,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        meal = NutritionMeal(
            public_id=MEAL_ID,
            patient_id=PATIENT_ID,
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
            public_id=FOOD_ID,
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

    reader_url, reader_cleanup = build_backend_reader_url(
        engine,
        "external_public_id_reader",
    )
    queries = BackendQueryTools(reader_url)
    chat = queries.validate_chat_message(
        ChatSyncRequest(
            request_id=CHAT_REQUEST_ID,
            message_id=CONFIRMATION_MESSAGE_ID,
            patient_id=PATIENT_ID,
            requested_return_type="text",
            message="조회해줘.",
            message_at=NOW,
        )
    )
    feedback = queries.validate_feedback_target(
        message_id=ASSISTANT_MESSAGE_ID,
        patient_id=PATIENT_ID,
    )
    dose_status = queries.medication_dose_status(
        patient_id=PATIENT_ID,
        target_date=NOW.date().isoformat(),
    )
    meals = queries.nutrition_meals(
        patient_id=PATIENT_ID,
        meal_date=NOW.date().isoformat(),
    )

    assert chat["recent_chat"][0]["message_id"] == CONFIRMATION_MESSAGE_ID
    assert feedback == {
        "message_id": ASSISTANT_MESSAGE_ID,
        "patient_id": PATIENT_ID,
        "role": "assistant",
    }
    assert dose_status["dose_events"][0]["dose_event_id"] == DOSE_EVENT_ID
    assert meals["meals"][0]["id"] == MEAL_ID
    assert meals["meals"][0]["foods"][0]["id"] == FOOD_ID
    assert queries.record_version(
        patient_id=PATIENT_ID,
        resource_type="medication_dose_event",
        record_id=DOSE_EVENT_ID,
    ) == 4
    assert queries.record_version(
        patient_id=PATIENT_ID,
        resource_type="nutrition_meal",
        record_id=MEAL_ID,
    ) == 2
    assert queries.record_version(
        patient_id=PATIENT_ID,
        resource_type="nutrition_food",
        record_id=FOOD_ID,
        parent_record_id=MEAL_ID,
    ) == 3
    with pytest.raises(ValidationError):
        ChatSyncRequest(
            request_id=CHAT_REQUEST_ID,
            message_id=str(user_internal_id),
            patient_id=PATIENT_ID,
            requested_return_type="text",
            message="조회해줘.",
            message_at=NOW,
        )
    with pytest.raises(BackendChatMessageNotFound):
        queries.validate_feedback_target(
            message_id=str(assistant_internal_id),
            patient_id=PATIENT_ID,
        )
    with pytest.raises(
        BackendRecordNotFound,
        match="medication_dose_event_not_found",
    ):
        queries.record_version(
            patient_id=PATIENT_ID,
            resource_type="medication_dose_event",
            record_id=str(dose_internal_id),
        )
    with pytest.raises(
        BackendRecordNotFound,
        match="nutrition_food_not_found",
    ):
        queries.record_version(
            patient_id=PATIENT_ID,
            resource_type="nutrition_food",
            record_id=str(food_internal_id),
            parent_record_id=str(meal_internal_id),
        )
    with pytest.raises(BackendChatMessageNotFound):
        queries.validate_feedback_target(
            message_id=CONFIRMATION_MESSAGE_ID,
            patient_id=PATIENT_ID,
        )
    queries.engine.dispose()
    reader_cleanup()
    cleanup()


def test_ai_read_boundary_recovers_complete_structured_reply_context(
    tmp_path: Path,
) -> None:
    engine, cleanup = build_system_engine(
        "structured_reply_read_context"
    )
    run_migrations(engine)
    sessions = sessionmaker(bind=engine, future=True)
    with sessions() as session:
        original_text = (
            "현미밥, 두부된장국, 시금치나물을 "
            "아침 식사로 기록해 주세요."
        )
        original = ChatMessage(
            public_id=ORIGIN_MESSAGE_ID,
            patient_id=CONTEXT_PATIENT_ID,
            ai_request_id=ORIGIN_REQUEST_ID,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content=original_text,
            message_payload_json=json.dumps(
                {"text": original_text},
                ensure_ascii=False,
            ),
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(original)
        session.flush()
        card = ChatMessage(
            public_id=CARD_MESSAGE_ID,
            patient_id=CONTEXT_PATIENT_ID,
            ai_request_id=ORIGIN_REQUEST_ID,
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type="selection_box",
            content="두부된장국과 일치하는 항목을 선택해 주세요.",
            message_payload_json=json.dumps(
                {
                    "message_title": "항목 선택",
                    "text": "두부된장국과 일치하는 항목을 선택해 주세요.",
                    "tables": None,
                    "selections": ["두부국", "소고기 두부국"],
                    "inputs": None,
                },
                ensure_ascii=False,
            ),
            reply_to_message_id=original.id,
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(card)
        session.flush()
        choice = ChatMessage(
            public_id=CHOICE_MESSAGE_ID,
            patient_id=CONTEXT_PATIENT_ID,
            ai_request_id=CHOICE_REQUEST_ID,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="selection_box",
            content="두부국",
            message_payload_json=json.dumps(
                {"text": "두부국"},
                ensure_ascii=False,
            ),
            reply_to_message_id=card.id,
            processing_status="pending",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(choice)
        session.commit()

    reader_url, reader_cleanup = build_backend_reader_url(
        engine,
        "structured_reply_context_reader",
    )
    queries = BackendQueryTools(reader_url)
    context = queries.validate_chat_message(
        ChatSyncRequest(
            request_id=CHOICE_REQUEST_ID,
            message_id=CHOICE_MESSAGE_ID,
            patient_id=CONTEXT_PATIENT_ID,
            requested_return_type="selection_box",
            message="두부국",
            message_at=NOW,
        )
    )

    assert context["recent_chat_complete"] is True
    assert [item["role"] for item in context["recent_chat"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert context["recent_chat"][1]["message"]["selections"] == [
        "두부국",
        "소고기 두부국",
    ]
    structured = context["structured_response_context"]
    assert structured["response_type"] == "selection_box"
    assert structured["response_value"] == "두부국"
    assert structured["source_message_id"] == CARD_MESSAGE_ID
    assert (
        structured["originating_user_message_id"]
        == ORIGIN_MESSAGE_ID
    )
    assert structured["source_message"]["message"]["selections"] == [
        "두부국",
        "소고기 두부국",
    ]
    assert (
        structured["originating_user_message"]["message_id"]
        == ORIGIN_MESSAGE_ID
    )
    assert (
        structured["originating_user_message"]["content"]
        == original_text
    )

    queries.engine.dispose()
    reader_cleanup()
    cleanup()


def test_model_visible_v13_entity_identifiers_are_opaque_strings() -> None:
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
            "source_chat_request_id": CHAT_REQUEST_ID,
            "confirmation_message_id": CONFIRMATION_MESSAGE_ID,
            "patient_id": PATIENT_ID,
            "resource_type": resource_type,
            "operation": operation,
            "record_id": record_id,
            "parent_record_id": parent_record_id,
            "expected_version": expected_version,
            "payload": payload,
            "requested_at": NOW.isoformat(),
        }
    )


def _authorize_record_approval(
    session,
    confirmation: ChatMessage,
    *,
    write_request_id: str,
    action_name: str,
    model_arguments: dict,
    expected_version: int | None,
) -> None:
    if confirmation.reply_to_message_id is None:
        card = ChatMessage(
            public_id=f"assistant_msg_{uuid4().hex[:16]}",
            patient_id=confirmation.patient_id,
            ai_request_id=f"req_{uuid4().hex[:16]}",
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type="selection_box",
            content="확인한 내용을 적용할까요?",
            message_payload_json=json.dumps(
                {
                    "message_title": "승인 확인",
                    "text": "확인한 내용을 적용할까요?",
                    "selections": ["변경 적용", "취소"],
                },
                ensure_ascii=False,
            ),
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(card)
        session.flush()
        confirmation.reply_to_message_id = card.id
        confirmation.message_type = "selection_box"
        confirmation.content = "변경 적용"
    session.flush()
