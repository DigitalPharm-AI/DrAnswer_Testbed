from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from agent_app.tools.backend_query import BackendChatMessageNotFound, BackendQueryTools
from shared.json_utils import dump_json
from shared.public_ids import new_public_id
from shared.tool_names import (
    CHANGE_NOTIFICATION_POLICY,
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    CREATE_NUTRITION_MEAL_RECORD,
    UPDATE_MEDICATION_DOSE_EVENT_STATUS,
)
from system_app import main as system_main
from system_app import security as system_security
from system_app.db import SessionLocal
from system_app.migrations import run_migrations
from system_app.models import (
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    MissedDoseFlag,
    NutritionMeal,
    ReminderPolicy,
    SideEffectRecord,
)
from system_app.openapi_v13 import build_backend_v13_write_openapi
from system_app.services.missed_dose_flag_service import activate_missed_dose_flag
from tests.helpers import (
    build_backend_reader_url,
    build_system_engine,
)

NOW = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)
BACKEND_HEADERS = {"Authorization": "Bearer pytest-agent-sync-token"}


def attach_backend_reply_edge(
    session,
    *,
    message: ChatMessage,
    write_request_id: str,
    action_name: str,
    arguments: dict,
    expected_version: int | None,
) -> None:
    """Create only the Backend-owned user reply edge."""

    action_label = {
        UPDATE_MEDICATION_DOSE_EVENT_STATUS: "기록",
        CREATE_MEDICATION_SIDE_EFFECT_RECORD: "기록",
        CREATE_NUTRITION_MEAL_RECORD: "기록",
        CHANGE_NOTIFICATION_POLICY: "변경",
    }.get(action_name, "변경 적용")
    card = ChatMessage(
        patient_id=message.patient_id,
        ai_request_id=new_public_id("request"),
        role="assistant",
        sender_type="assistant",
        category="multiturn_chat",
        message_type="selection_box",
        content="확인한 내용을 적용할까요?",
        message_payload_json=dump_json(
            {
                "message_title": "승인 확인",
                "text": "확인한 내용을 적용할까요?",
                "selections": [action_label, "취소"],
            }
        ),
        processing_status="completed",
        created_at=NOW.replace(tzinfo=None),
    )
    session.add(card)
    session.flush()
    message.reply_to_message_id = card.id
    message.message_type = "selection_box"
    message.content = action_label

    session.flush()
    return None


def test_backend_v13_openapi_declares_bearer_and_contract_error_responses() -> None:
    specification = system_main.create_app().openapi()
    record_operation = specification["paths"]["/agent/sync/record-change"]["post"]
    policy_operation = specification["paths"]["/agent/sync/notification-policy-change"]["post"]

    assert specification["components"]["securitySchemes"]["BackendApiBearer"]["scheme"] == "bearer"
    assert record_operation["security"] == [{"BackendApiBearer": []}]
    assert policy_operation["security"] == [{"BackendApiBearer": []}]
    assert "parameters" not in record_operation
    assert "conversation_id" not in specification["components"]["schemas"][
        "RecordChangeRequest"
    ]["properties"]
    assert "conversation_id" not in specification["components"]["schemas"][
        "NotificationPolicyChangeRequest"
    ]["properties"]
    assert set(record_operation["responses"]) == {
        "200", "400", "401", "404", "409", "422", "500"
    }
    assert set(policy_operation["responses"]) == {
        "200", "400", "401", "404", "409", "422", "500"
    }
    assert (
        policy_operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/CommonErrorResponse"
    )
    record_request = specification["components"]["schemas"][
        "RecordChangeRequest"
    ]
    assert {
        "record_id",
        "parent_record_id",
        "expected_version",
        "payload",
    } <= set(record_request["required"])
    assert set(
        specification["components"]["schemas"][
            "RecordChangeResponse"
        ]["properties"]
    ) == {"request_id", "result", "processed_at"}
    assert set(
        specification["components"]["schemas"][
            "NotificationPolicyChangeResponse"
        ]["properties"]
    ) == {"request_id", "result", "processed_at"}
    assert set(
        specification["components"]["schemas"][
            "CommonErrorResponse"
        ]["properties"]
    ) == {"request_id", "error"}
    exported = json.loads(Path("docs/BACKEND_V13_WRITE_OPENAPI.json").read_text(encoding="utf-8"))
    assert exported == build_backend_v13_write_openapi(system_main.create_app())
    assert "/api/chat/sync" not in specification["paths"]
    assert "/api/ui/v1/chat/sync" in specification["paths"]
    assert not any(
        path.endswith("/execute")
        for path in specification["paths"]
    )


def test_backend_v13_invalid_request_and_unauthorized_use_contract_error_shape(monkeypatch) -> None:
    invalid_request_id = new_public_id("request")
    invalid_client = TestClient(system_main.app)
    invalid = invalid_client.post(
        "/agent/sync/notification-policy-change",
        json={"request_id": invalid_request_id},
        headers=BACKEND_HEADERS,
    )
    invalid_client.close()

    assert invalid.status_code == 400
    assert set(invalid.json()) == {"request_id", "error"}
    assert invalid.json()["request_id"] == invalid_request_id
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.json()["error"]["details"]["violations"]

    class AuthenticatedSettings:
        agent_sync_api_token = "agent-sync-secret"
        internal_api_token = "internal-secret"

        @staticmethod
        def require_service_api_token() -> str:
            return "agent-sync-secret"

    monkeypatch.setattr(system_security, "get_settings", lambda: AuthenticatedSettings())
    unauthorized_request_id = new_public_id("request")
    valid_body = {
        "request_id": unauthorized_request_id,
        "source_chat_request_id": new_public_id("request"),
        "confirmation_message_id": new_public_id("user_message"),
        "patient_id": new_public_id("patient"),
        "resource_type": "nutrition_meal",
        "operation": "create",
        "record_id": None,
        "parent_record_id": None,
        "expected_version": None,
        "payload": {
            "meal_type": "lunch",
            "foods": [{"food_name": "두부", "nutrients": {"protein": 8}}],
            "reason": "AI Server Tool 실행",
        },
        "requested_at": NOW.isoformat(),
    }
    auth_client = TestClient(system_main.app)
    missing = auth_client.post("/agent/sync/record-change", json=valid_body)
    wrong = auth_client.post(
        "/agent/sync/record-change",
        json=valid_body,
        headers={"Authorization": "Bearer wrong-secret"},
    )
    agent_direction = auth_client.post(
        "/agent/sync/record-change",
        json=valid_body,
        headers={"Authorization": "Bearer agent-sync-secret"},
    )
    internal_fallback = auth_client.post(
        "/agent/sync/record-change",
        json=valid_body,
        headers={"Authorization": "Bearer internal-secret"},
    )
    auth_client.close()

    for unauthorized in (missing, wrong, internal_fallback):
        assert unauthorized.status_code == 401
        assert set(unauthorized.json()) == {"request_id", "error"}
        assert unauthorized.json()["request_id"] == unauthorized_request_id
        assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    assert agent_direction.status_code != 401


def test_backend_record_change_enforces_confirmation_version_and_idempotency() -> None:
    patient_id = new_public_id("patient")
    source_request_id = new_public_id("request")
    write_request_id = new_public_id("request")
    with SessionLocal() as session:
        source = ChatMessage(
            patient_id=patient_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="아침 약을 복용했어.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(source)
        session.flush()
        mismatched_confirmation = ChatMessage(
            patient_id=patient_id,
            ai_request_id=new_public_id("request"),
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="Apply a different request.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(mismatched_confirmation)
        plan = MedicationPlan(
            patient_id=patient_id,
            medication_name="테스트약",
            dosage="1정",
            start_date=NOW.date(),
            end_date=NOW.date(),
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(plan)
        session.flush()
        schedule = DoseSchedule(
            plan_id=plan.id,
            slot_label="아침",
            scheduled_time="08:00",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(schedule)
        session.flush()
        event = DoseEvent(
            patient_id=patient_id,
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
        missed_event = DoseEvent(
            patient_id=patient_id,
            plan_id=plan.id,
            schedule_id=schedule.id,
            medication_name=plan.medication_name,
            slot_label=schedule.slot_label,
            scheduled_for=NOW.replace(
                hour=8,
                minute=0,
                tzinfo=None,
            ),
            status="missed",
            missed_handled=True,
            missed_detected_at=NOW.replace(
                hour=9,
                minute=30,
                tzinfo=None,
            ),
            version=1,
            created_at=NOW.replace(tzinfo=None),
            updated_at=NOW.replace(tzinfo=None),
        )
        session.add_all([event, missed_event])
        session.flush()
        flag = activate_missed_dose_flag(
            session,
            missed_event,
            activated_at=missed_event.missed_detected_at,
        )
        attach_backend_reply_edge(
            session,
            message=source,
            write_request_id=write_request_id,
            action_name=UPDATE_MEDICATION_DOSE_EVENT_STATUS,
            arguments={
                "dose_event_id": event.public_id,
                "status": "taken",
                "taken_at": NOW.isoformat(),
                "reason": "사용자 확인",
            },
            expected_version=1,
        )
        session.commit()
        confirmation_id = source.public_id
        mismatched_confirmation_id = mismatched_confirmation.public_id
        event_id = event.id
        event_public_id = event.public_id
        flag_id = flag.id

    body = {
        "request_id": write_request_id,
        "source_chat_request_id": source_request_id,
        "confirmation_message_id": confirmation_id,
        "patient_id": patient_id,
        "resource_type": "medication_dose_event",
        "operation": "update",
        "record_id": event_public_id,
        "parent_record_id": None,
        "expected_version": 1,
        "payload": {
            "status": "taken",
            "taken_at": NOW.isoformat(),
            "reason": "사용자 확인",
        },
        "requested_at": NOW.isoformat(),
    }
    client = TestClient(system_main.app)
    mismatched_context = client.post(
        "/agent/sync/record-change",
        json={
            **body,
            "request_id": new_public_id("request"),
            "confirmation_message_id": mismatched_confirmation_id,
        },
        headers=BACKEND_HEADERS,
    )
    first = client.post(
        "/agent/sync/record-change",
        json=body,
        headers=BACKEND_HEADERS,
    )
    replay = client.post(
        "/agent/sync/record-change",
        json=body,
        headers=BACKEND_HEADERS,
    )
    changed = {**body, "payload": {**body["payload"], "reason": "다른 본문"}}
    conflict = client.post(
        "/agent/sync/record-change",
        json=changed,
        headers=BACKEND_HEADERS,
    )
    client.close()

    assert mismatched_context.status_code == 409
    assert mismatched_context.json()["error"]["code"] == "SOURCE_CHAT_REQUEST_NOT_FOUND"
    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["result"]["version"] == 2
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with SessionLocal() as session:
        event = session.get(DoseEvent, event_id)
        assert event is not None
        assert event.status == "taken"
        assert event.version == 2
        assert event.taken_at == datetime(2026, 7, 25, 22, 0)
        assert event.taken_at.tzinfo is None
        flag = session.get(MissedDoseFlag, flag_id)
        assert flag is not None
        assert flag.active is False
        assert flag.clear_reason == "subsequent_same_day_taken"


def test_backend_nutrition_write_uses_v13_record_change_only() -> None:
    patient_id = new_public_id("patient")
    source_request_id = new_public_id("request")
    write_request_id = new_public_id("request")
    model_arguments = {
        "meal_type": "lunch",
        "meal_date": "2026-07-25",
        "meal_time": "12:30",
        "description": "두부 점심",
        "foods": [
            {
                "food_name": "두부",
                "portion": "1모",
                "nutrients": {
                    "calories": 160,
                    "protein": 16,
                    "sodium": 20,
                    "fat": 9,
                    "carbohydrates": 4,
                },
            }
        ],
    }
    with SessionLocal() as session:
        confirmation = ChatMessage(
            patient_id=patient_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="selection_box",
            content="기록",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(confirmation)
        session.flush()
        attach_backend_reply_edge(
            session,
            message=confirmation,
            write_request_id=write_request_id,
            action_name=CREATE_NUTRITION_MEAL_RECORD,
            arguments=model_arguments,
            expected_version=None,
        )
        session.commit()
        confirmation_id = confirmation.public_id

    body = {
        "request_id": write_request_id,
        "source_chat_request_id": source_request_id,
        "confirmation_message_id": confirmation_id,
        "patient_id": patient_id,
        "resource_type": "nutrition_meal",
        "operation": "create",
        "record_id": None,
        "parent_record_id": None,
        "expected_version": None,
        "payload": model_arguments,
        "requested_at": NOW.isoformat(),
    }
    with TestClient(system_main.app) as client:
        response = client.post(
            "/agent/sync/record-change",
            json=body,
            headers=BACKEND_HEADERS,
        )

    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["resource_type"] == "nutrition_meal"
    assert result["operation"] == "create"
    assert result["record_id"].startswith("meal_")
    assert result["version"] == 1
    with SessionLocal() as session:
        meal = session.scalar(
            select(NutritionMeal).where(
                NutritionMeal.public_id == result["record_id"],
                NutritionMeal.patient_id == patient_id,
            )
        )
        assert meal is not None
        assert meal.meal_type == "lunch"
        assert meal.description == "두부 점심"


def test_backend_side_effect_create_is_confirmed_public_and_idempotent() -> None:
    patient_id = new_public_id("patient")
    source_request_id = new_public_id("request")
    write_request_id = new_public_id("request")
    authoritative_arguments = {
        "medication_name": "암로디핀 5mg",
        "symptom_text": "어제 약을 먹고 속이 메스꺼웠어",
        "symptom_onset_text": "어제 복용 후",
        "suspected": True,
        "severity": {
            "questions": [
                {
                    "item_code": "PROCTCAE_NAUSEA",
                    "question": (
                        "지난 7일 동안 메스꺼움의 정도는 어떠했습니까?"
                    ),
                    "response_type": "single_choice",
                    "response_options": [
                        "없음",
                        "약간",
                        "중간 정도",
                        "심함",
                        "매우 심함",
                    ],
                }
            ],
            "responses": [
                {
                    "item_code": "PROCTCAE_NAUSEA",
                    "response_index": 2,
                    "response_text": "중간 정도",
                }
            ],
        },
        "matched_effects": ["암로디핀 5mg: 메스꺼움"],
        "matched_items": ["암로디핀 5mg"],
        "related_dose_event_id": None,
    }
    with SessionLocal() as session:
        confirmation = ChatMessage(
            patient_id=patient_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="selection_box",
            content="기록",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(confirmation)
        session.flush()
        attach_backend_reply_edge(
            session,
            message=confirmation,
            write_request_id=write_request_id,
            action_name=CREATE_MEDICATION_SIDE_EFFECT_RECORD,
            arguments=authoritative_arguments,
            expected_version=None,
        )
        session.commit()
        confirmation_id = confirmation.public_id

    body = {
        "request_id": write_request_id,
        "source_chat_request_id": source_request_id,
        "confirmation_message_id": confirmation_id,
        "patient_id": patient_id,
        "resource_type": "medication_side_effect",
        "operation": "create",
        "record_id": None,
        "parent_record_id": None,
        "expected_version": None,
        "payload": authoritative_arguments,
        "requested_at": NOW.isoformat(),
    }
    client = TestClient(system_main.app)
    first = client.post(
        "/agent/sync/record-change",
        json=body,
        headers=BACKEND_HEADERS,
    )
    replay = client.post(
        "/agent/sync/record-change",
        json=body,
        headers=BACKEND_HEADERS,
    )
    conflict = client.post(
        "/agent/sync/record-change",
        json={
            **body,
            "payload": {
                **body["payload"],
                "symptom_text": "서로 다른 본문",
            },
        },
        headers=BACKEND_HEADERS,
    )
    client.close()

    assert first.status_code == 200, first.text
    assert replay.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["result"]["record_id"].startswith("sidefx_")
    assert first.json()["result"]["version"] == 1
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    with SessionLocal() as session:
        records = list(
            session.scalars(
                select(SideEffectRecord).where(
                    SideEffectRecord.patient_id == patient_id
                )
            ).all()
        )
        assert len(records) == 1
        assert records[0].public_id == first.json()["result"]["record_id"]
        assert records[0].version == 1
        assert json.loads(records[0].severity_result_json) == (
            authoritative_arguments["severity"]
        )
        assert records[0].evidence == ""
        assert records[0].recommendation == ""
        assert not hasattr(records[0], "source_trace_id")
        assert records[0].metadata_json == "{}"


def test_backend_notification_policy_change_uses_public_id_and_enforces_boundaries() -> None:
    patient_id = new_public_id("patient")
    source_request_id = new_public_id("request")
    write_request_id = new_public_id("request")
    with SessionLocal() as session:
        source = ChatMessage(
            patient_id=patient_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="아침 알림을 바꿔줘.",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        policy = ReminderPolicy(
            patient_id=patient_id,
            policy_key="morning-dose",
            slot_label="아침",
            extra_reminders=1,
            interval_minutes=15,
            missed_dose_after_minutes=90,
            primary_reminder_timing="at",
            primary_reminder_offset_minutes=0,
            effective_start_date=date(2026, 7, 25),
            effective_end_date=date(2026, 8, 1),
            reason="초기 정책",
            source="test",
            active=True,
            version=1,
        )
        session.add_all([source, policy])
        session.flush()
        apply_arguments = {
            "policy_id": policy.public_id,
            "decision": "apply",
            "changes": {
                "extra_reminders": 2,
                "interval_minutes": 15,
            },
            "reason": "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행",
        }
        attach_backend_reply_edge(
            session,
            message=source,
            write_request_id=write_request_id,
            action_name=CHANGE_NOTIFICATION_POLICY,
            arguments=apply_arguments,
            expected_version=1,
        )
        session.commit()
        confirmation_id = source.public_id
        policy_public_id = policy.public_id
        policy_db_id = policy.id

    body = {
        "request_id": write_request_id,
        "source_chat_request_id": source_request_id,
        "confirmation_message_id": confirmation_id,
        "patient_id": patient_id,
        "policy_id": policy_public_id,
        "expected_version": 1,
        "payload": {
            "decision": "apply",
            "changes": {
                "extra_reminders": 2,
                "interval_minutes": 15,
            },
            "reason": "사용자 채팅 메시지에서 명시적으로 확인된 AI Tool 실행",
        },
        "requested_at": NOW.isoformat(),
    }
    client = TestClient(system_main.app)
    applied = client.post(
        "/agent/sync/notification-policy-change",
        json=body,
        headers=BACKEND_HEADERS,
    )
    numeric_request_id = new_public_id("request")
    numeric_id_attempt = client.post(
        "/agent/sync/notification-policy-change",
        json={
            **body,
            "request_id": numeric_request_id,
            "policy_id": str(policy_db_id),
            "expected_version": 2,
            "payload": {
                "decision": "keep",
                "changes": None,
                "reason": "기존 정책 유지",
            },
        },
        headers=BACKEND_HEADERS,
    )

    boundary_source_request_id = new_public_id("request")
    boundary_write_request_id = new_public_id("request")
    boundary_payload = {
        "decision": "apply",
        "changes": {
            "extra_reminders": 5,
            "interval_minutes": 60,
            "missed_dose_after_minutes": 15,
        },
        "reason": "경계 검증",
    }
    with SessionLocal() as session:
        boundary_confirmation = ChatMessage(
            patient_id=patient_id,
            ai_request_id=boundary_source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="selection_box",
            content="변경 적용",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(boundary_confirmation)
        session.flush()
        attach_backend_reply_edge(
            session,
            message=boundary_confirmation,
            write_request_id=boundary_write_request_id,
            action_name=CHANGE_NOTIFICATION_POLICY,
            arguments={
                "policy_id": policy_public_id,
                **boundary_payload,
            },
            expected_version=2,
        )
        session.commit()
        boundary_confirmation_id = boundary_confirmation.public_id

    boundary_violation = client.post(
        "/agent/sync/notification-policy-change",
        json={
            **body,
            "request_id": boundary_write_request_id,
            "source_chat_request_id": boundary_source_request_id,
            "confirmation_message_id": boundary_confirmation_id,
            "expected_version": 2,
            "payload": boundary_payload,
        },
        headers=BACKEND_HEADERS,
    )
    client.close()

    assert applied.status_code == 200
    assert applied.json()["result"]["policy_id"] == policy_public_id
    assert applied.json()["result"]["version"] == 2
    assert set(applied.json()["result"]) == {
        "policy_id",
        "decision",
        "version",
    }
    assert numeric_id_attempt.status_code == 400
    assert numeric_id_attempt.json()["request_id"] == numeric_request_id
    assert numeric_id_attempt.json()["error"]["code"] == "INVALID_REQUEST"
    assert boundary_violation.status_code == 422
    assert boundary_violation.json()["error"]["code"] == "POLICY_BOUNDARY_VIOLATION"
    assert boundary_violation.json()["error"]["details"]["policy_id"] == policy_public_id
    assert boundary_violation.json()["error"]["details"]["violations"]
    with SessionLocal() as session:
        changed = session.get(ReminderPolicy, policy_db_id)
        assert changed is not None
        assert changed.public_id == policy_public_id
        assert changed.extra_reminders == 2
        assert changed.interval_minutes == 15
        assert changed.version == 2


def test_backend_query_tools_verify_message_and_enforce_postgresql_query_only(
    tmp_path: Path,
) -> None:
    patient_id = new_public_id("patient")
    request_id = new_public_id("request")
    engine, cleanup = build_system_engine("system_backend_read")
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    run_migrations(engine)
    with sessions() as session:
        message = ChatMessage(
            patient_id=patient_id,
            ai_request_id=request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="조회 테스트",
            processing_status="pending",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(message)
        policy = ReminderPolicy(
            patient_id=patient_id,
            policy_key="morning-dose",
            slot_label="아침",
            extra_reminders=1,
            interval_minutes=15,
            missed_dose_after_minutes=90,
            primary_reminder_timing="at",
            primary_reminder_offset_minutes=0,
            effective_start_date=date(2026, 7, 25),
            effective_end_date=date(2026, 8, 1),
            reason="조회 테스트",
            source="test",
            active=True,
            version=3,
        )
        session.add(policy)
        session.commit()
        message_public_id = message.public_id
        policy_public_id = policy.public_id

    reader_url, reader_cleanup = build_backend_reader_url(
        engine,
        "system_backend_reader",
    )
    queries = BackendQueryTools(reader_url)
    from shared.chat_contracts import ChatSyncRequest

    request = ChatSyncRequest(
        request_id=request_id,
        message_id=message_public_id,
        patient_id=patient_id,
        requested_return_type="text",
        message="조회 테스트",
        message_at=NOW,
    )
    context = queries.validate_chat_message(request)
    assert context["backend_message_verified"] is True
    assert context["recent_chat"][0]["message_id"] == message_public_id
    policies = queries.notification_policies(patient_id=patient_id)
    assert policies["policies"][0]["policy_id"] == policy_public_id
    assert policies["policies"][0]["version"] == 3
    assert queries.notification_policy_version(
        patient_id=patient_id,
        policy_id=policy_public_id,
    ) == 3

    with pytest.raises(BackendChatMessageNotFound):
        queries.validate_chat_message(request.model_copy(update={"message": "위조된 본문"}))
    with pytest.raises(DBAPIError):
        with queries.engine.begin() as connection:
            connection.execute(
                insert(ChatMessage).values(
                    patient_id="write-denied",
                    ai_request_id="write-denied",
                    role="user",
                    sender_type="patient",
                    category="chat",
                    message_type="text",
                    content="금지",
                    message_payload_json="{}",
                    processing_status="completed",
                    metadata_json="{}",
                    created_at=NOW.replace(tzinfo=None),
                )
            )
    queries.engine.dispose()
    reader_cleanup()
    cleanup()
