from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert, select
from sqlalchemy.exc import OperationalError

from agent_app.integration.chat_contracts import ChatMessageContent, ChatSyncResponse
from agent_app.tools.backend_query import BackendChatMessageNotFound, BackendQueryTools
from shared.db import create_session_factory
from shared.settings import get_settings
from system_app import main as system_main
from system_app import security as system_security
from system_app.db import SessionLocal
from system_app.migrations import run_migrations
from system_app.models import (
    Base,
    ChatMessage,
    DoseEvent,
    DoseSchedule,
    MedicationPlan,
    ReminderPolicy,
)
from system_app.openapi_v12 import build_backend_v12_write_openapi
from system_app.routes import chat as chat_routes

NOW = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)
BACKEND_HEADERS = {"Authorization": "Bearer pytest-backend-api-token"}


def seed_pending_contract_response(
    *,
    conversation_id: str,
    source_request_id: str,
    message_type: str,
    message_payload: dict,
) -> int:
    patient_id = get_settings().patient_id
    with SessionLocal() as session:
        source_user = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
            ai_request_id=source_request_id,
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="구조화 응답을 요청합니다.",
            message_payload_json=json.dumps(
                {"text": "구조화 응답을 요청합니다."},
                ensure_ascii=False,
            ),
            processing_status="completed",
            metadata_json="{}",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(source_user)
        session.flush()
        assistant = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
            ai_request_id=source_request_id,
            role="assistant",
            sender_type="assistant",
            category="multiturn_chat",
            message_type=message_type,
            content=str(message_payload.get("text") or message_payload.get("message_title") or ""),
            message_payload_json=json.dumps(message_payload, ensure_ascii=False),
            reply_to_message_id=source_user.id,
            processing_status="completed",
            metadata_json=json.dumps(
                {
                    "message_at": NOW.isoformat(),
                    "pending_response_type": message_type,
                    "pending_response_status": "pending",
                }
            ),
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(assistant)
        session.commit()
        return assistant.id


def test_backend_v12_openapi_declares_bearer_and_contract_error_responses() -> None:
    specification = system_main.create_app().openapi()
    record_operation = specification["paths"]["/agent/sync/record-change"]["post"]
    policy_operation = specification["paths"]["/agent/sync/notification-policy-change"]["post"]

    assert specification["components"]["securitySchemes"]["BackendApiBearer"]["scheme"] == "bearer"
    assert record_operation["security"] == [{"BackendApiBearer": []}]
    assert policy_operation["security"] == [{"BackendApiBearer": []}]
    assert "parameters" not in record_operation
    assert set(record_operation["responses"]) == {"200", "401", "404", "409", "422", "500", "503", "504"}
    assert set(policy_operation["responses"]) == {"200", "401", "404", "409", "422", "500", "503", "504"}
    assert (
        policy_operation["responses"]["422"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/NotificationPolicyChangeResponse"
    )
    exported = json.loads(Path("docs/BACKEND_V12_WRITE_OPENAPI.json").read_text(encoding="utf-8"))
    assert exported == build_backend_v12_write_openapi(system_main.create_app())


def test_backend_v12_invalid_request_and_unauthorized_use_contract_error_shape(monkeypatch) -> None:
    invalid_client = TestClient(system_main.app)
    invalid = invalid_client.post(
        "/agent/sync/notification-policy-change",
        json={"request_id": "invalid-policy-request"},
        headers=BACKEND_HEADERS,
    )
    invalid_client.close()

    assert invalid.status_code == 422
    assert invalid.json()["success"] is False
    assert invalid.json()["request_id"] == "invalid-policy-request"
    assert invalid.json()["result"] is None
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid.json()["error"]["details"]["violations"]

    class AuthenticatedSettings:
        backend_api_token = "backend-secret"
        agent_sync_api_token = "agent-sync-secret"
        internal_api_token = "internal-secret"

        @staticmethod
        def require_backend_api_token() -> str:
            return "backend-secret"

    monkeypatch.setattr(system_security, "get_settings", lambda: AuthenticatedSettings())
    valid_body = {
        "request_id": "unauthorized-record-request",
        "source_chat_request_id": "source-request",
        "conversation_id": "conversation",
        "confirmation_message_id": "100",
        "patient_id": "patient",
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

    for unauthorized in (missing, wrong, agent_direction, internal_fallback):
        assert unauthorized.status_code == 401
        assert unauthorized.json()["success"] is False
        assert unauthorized.json()["request_id"] == "unauthorized-record-request"
        assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"


def test_backend_chat_persists_user_before_agent_and_replays_without_duplicate(monkeypatch) -> None:
    suffix = uuid4().hex
    request_id = f"backend-chat-{suffix}"
    conversation_id = f"conversation-{suffix}"
    patient_id = f"patient-{suffix}"
    calls = 0

    async def fake_send_sync_chat(payload):
        nonlocal calls
        calls += 1
        with SessionLocal() as session:
            persisted = session.scalar(
                select(ChatMessage).where(ChatMessage.public_id == payload.message_id)
            )
            assert persisted is not None
            assert persisted.role == "user"
            assert persisted.processing_status == "pending"
            assert persisted.ai_request_id == request_id
        return ChatSyncResponse(
            request_id=payload.request_id,
            message_id=payload.message_id,
            message_type="text",
            message=ChatMessageContent(
                message_title=None,
                text="Backend 저장 이후 생성된 AI 답변",
                tables=None,
                selections=None,
                inputs=None,
            ),
            message_at=NOW,
        )

    monkeypatch.setattr(system_main.agent_client, "send_sync_chat", fake_send_sync_chat)
    body = {
        "request_id": request_id,
        "conversation_id": conversation_id,
        "patient_id": patient_id,
        "message": "내 복약 기록을 확인해줘.",
        "requested_return_type": None,
        "message_at": NOW.isoformat(),
    }
    client = TestClient(system_main.app)
    first = client.post("/api/chat/sync", json=body)
    second = client.post("/api/chat/sync", json=body)
    client.close()

    assert first.status_code == 200, first.text
    assert second.status_code == 200
    assert first.json() == second.json()
    assert calls == 1
    assert first.json()["user_message_id"] != first.json()["assistant_message_id"]
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.ai_request_id == request_id)
                .order_by(ChatMessage.id)
            ).all()
        )
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[0].processing_status == "completed"
        assert rows[1].reply_to_message_id == rows[0].id
        assert rows[1].message_type == "text"


def test_backend_chat_matches_next_user_response_to_pending_type(monkeypatch) -> None:
    suffix = uuid4().hex
    conversation_id = f"pending-conversation-{suffix}"
    source_request_id = f"pending-source-{suffix}"
    patient_id = get_settings().patient_id
    seed_pending_contract_response(
        conversation_id=conversation_id,
        source_request_id=source_request_id,
        message_type="selection_box",
        message_payload={
            "message_title": "복용 여부",
            "text": "약을 복용했나요?",
            "tables": None,
            "selections": ["복용함", "복용하지 않음"],
            "inputs": None,
        },
    )

    async def fake_send_sync_chat(payload):
        assert payload.requested_return_type == "selection_box"
        return ChatSyncResponse(
            request_id=payload.request_id,
            message_id=payload.message_id,
            message_type="text",
            message=ChatMessageContent(
                message_title=None,
                text="응답을 확인했습니다.",
                tables=None,
                selections=None,
                inputs=None,
            ),
            message_at=NOW,
        )

    monkeypatch.setattr(system_main.agent_client, "send_sync_chat", fake_send_sync_chat)
    client = TestClient(system_main.app)
    mismatch = client.post(
        "/api/chat/sync",
        json={
            "request_id": f"pending-mismatch-{suffix}",
            "conversation_id": conversation_id,
            "patient_id": patient_id,
            "message": "복용함",
            "requested_return_type": None,
            "message_at": NOW.isoformat(),
        },
    )
    accepted = client.post(
        "/api/chat/sync",
        json={
            "request_id": f"pending-accepted-{suffix}",
            "conversation_id": conversation_id,
            "patient_id": patient_id,
            "message": "복용함",
            "requested_return_type": "selection_box",
            "message_at": NOW.isoformat(),
        },
    )
    client.close()

    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["code"] == "REQUESTED_RETURN_TYPE_MISMATCH"
    assert accepted.status_code == 200
    with SessionLocal() as session:
        submitted = session.scalar(
            select(ChatMessage).where(
                ChatMessage.ai_request_id == f"pending-accepted-{suffix}",
                ChatMessage.role == "user",
            )
        )
        assert submitted is not None
        assert submitted.message_type == "selection_box"
        metadata = json.loads(submitted.metadata_json)
        assert metadata["source_chat_request_id"] == source_request_id


def test_backend_record_change_enforces_confirmation_version_and_idempotency() -> None:
    suffix = uuid4().hex
    patient_id = f"patient-{suffix}"
    conversation_id = f"conversation-{suffix}"
    source_request_id = f"source-{suffix}"
    write_request_id = f"write-{suffix}"
    with SessionLocal() as session:
        source = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
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
            conversation_id=conversation_id,
            ai_request_id=f"different-source-{suffix}",
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
        session.add(event)
        session.commit()
        confirmation_id = source.id
        mismatched_confirmation_id = mismatched_confirmation.id
        event_id = event.id

    body = {
        "request_id": write_request_id,
        "source_chat_request_id": source_request_id,
        "conversation_id": conversation_id,
        "confirmation_message_id": str(confirmation_id),
        "patient_id": patient_id,
        "resource_type": "medication_dose_event",
        "operation": "update",
        "record_id": str(event_id),
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
            "request_id": f"write-mismatched-confirmation-{suffix}",
            "confirmation_message_id": str(mismatched_confirmation_id),
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


def test_backend_notification_policy_change_uses_public_id_and_enforces_boundaries() -> None:
    suffix = uuid4().hex
    patient_id = f"patient-policy-{suffix}"
    conversation_id = f"conversation-policy-{suffix}"
    source_request_id = f"source-policy-{suffix}"
    with SessionLocal() as session:
        source = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
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
        session.commit()
        confirmation_id = source.id
        policy_public_id = policy.public_id
        policy_db_id = policy.id

    body = {
        "request_id": f"write-policy-{suffix}",
        "source_chat_request_id": source_request_id,
        "conversation_id": conversation_id,
        "confirmation_message_id": str(confirmation_id),
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
    numeric_id_attempt = client.post(
        "/agent/sync/notification-policy-change",
        json={
            **body,
            "request_id": f"write-policy-numeric-{suffix}",
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
    boundary_violation = client.post(
        "/agent/sync/notification-policy-change",
        json={
            **body,
            "request_id": f"write-policy-boundary-{suffix}",
            "expected_version": 2,
            "payload": {
                "decision": "apply",
                "changes": {
                    "extra_reminders": 5,
                    "interval_minutes": 60,
                    "missed_dose_after_minutes": 15,
                },
                "reason": "경계 검증",
            },
        },
        headers=BACKEND_HEADERS,
    )
    client.close()

    assert applied.status_code == 200
    assert applied.json()["result"]["policy_id"] == policy_public_id
    assert applied.json()["result"]["version"] == 2
    assert numeric_id_attempt.status_code == 404
    assert numeric_id_attempt.json()["error"]["code"] == "notification_policy_not_found"
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


def test_backend_query_tools_verify_message_and_enforce_sqlite_query_only(tmp_path: Path) -> None:
    database_path = (tmp_path / "backend-read.db").as_posix()
    engine, sessions = create_session_factory(f"sqlite:///{database_path}")
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    with sessions() as session:
        message = ChatMessage(
            patient_id="patient-read",
            conversation_id="conversation-read",
            ai_request_id="request-read",
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
            patient_id="patient-read",
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
        message_id = message.id
        message_public_id = message.public_id
        policy_public_id = policy.public_id

    queries = BackendQueryTools(f"sqlite:///{database_path}")
    from agent_app.integration.chat_contracts import ChatSyncRequest

    request = ChatSyncRequest(
        request_id="request-read",
        message_id=str(message_id),
        conversation_id="conversation-read",
        patient_id="patient-read",
        requested_return_type=None,
        message="조회 테스트",
        message_at=NOW,
    )
    context = queries.validate_chat_message(request)
    assert context["backend_message_verified"] is True
    assert context["recent_chat"][0]["message_id"] == message_public_id
    policies = queries.notification_policies(patient_id="patient-read")
    assert policies["policies"][0]["policy_id"] == policy_public_id
    assert policies["policies"][0]["version"] == 3
    assert queries.notification_policy_version(
        patient_id="patient-read",
        policy_id=policy_public_id,
    ) == 3

    with pytest.raises(BackendChatMessageNotFound):
        queries.validate_chat_message(request.model_copy(update={"message": "위조된 본문"}))
    with pytest.raises(OperationalError):
        with queries.engine.begin() as connection:
            connection.execute(
                insert(ChatMessage).values(
                    patient_id="write-denied",
                    conversation_id="write-denied",
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
    engine.dispose()
    queries.engine.dispose()


def test_ui_contract_input_response_keeps_conversation_and_uses_v12_worker(monkeypatch) -> None:
    suffix = uuid4().hex
    conversation_id = f"ui-conversation-{suffix}"
    source_request_id = f"ui-source-{suffix}"
    captured_thread: dict = {}

    def capture_thread(*, name, target, args):
        captured_thread.update({"name": name, "target": target, "args": args})

    source_assistant_id = seed_pending_contract_response(
        conversation_id=conversation_id,
        source_request_id=source_request_id,
        message_type="input_box",
        message_payload={
            "message_title": "추가 정보",
            "text": "체중과 식사 시점을 입력해주세요.",
            "tables": None,
            "selections": None,
            "inputs": [
                {
                    "type": "number",
                    "label": "체중",
                    "value": None,
                    "options": {
                        "unit": "kg",
                        "lower": 1,
                        "upper": 300,
                        "selections": None,
                    },
                },
                {
                    "type": "dropdown",
                    "label": "식사",
                    "value": None,
                    "options": {
                        "unit": None,
                        "lower": None,
                        "upper": None,
                        "selections": ["아침", "점심", "저녁"],
                    },
                },
            ],
        },
    )

    async def fake_send_sync_chat(payload):
        assert payload.conversation_id == conversation_id
        assert payload.requested_return_type == "input_box"
        assert payload.message == '{"체중":"70","식사":"점심"}'
        return ChatSyncResponse(
            request_id=payload.request_id,
            message_id=payload.message_id,
            message_type="text",
            message=ChatMessageContent(
                message_title=None,
                text="입력값을 확인했습니다.",
                tables=None,
                selections=None,
                inputs=None,
            ),
            message_at=NOW,
        )

    monkeypatch.setattr(chat_routes, "start_daemon_thread", capture_thread)
    monkeypatch.setattr(system_main.agent_client, "send_sync_chat", fake_send_sync_chat)
    client = TestClient(system_main.app)
    invalid_fields = client.post(
        "/chat/contract-response",
        data={
            "conversation_id": conversation_id,
            "source_chat_request_id": source_request_id,
            "response_kind": "input",
            "input_labels": ["체중"],
            "input_values": ["70"],
        },
    )
    response = client.post(
        "/chat/contract-response",
        data={
            "conversation_id": conversation_id,
            "source_chat_request_id": source_request_id,
            "response_kind": "input",
            "input_labels": ["체중", "식사"],
            "input_values": ["70", "점심"],
        },
    )
    client.close()

    assert invalid_fields.status_code == 409
    assert invalid_fields.json()["detail"] == "INPUT_BOX_FIELDS_MISMATCH"
    assert response.status_code == 200
    assert captured_thread["name"].startswith("system-event-request-")
    captured_thread["target"](*captured_thread["args"])
    with SessionLocal() as session:
        rows = list(
            session.scalars(
                select(ChatMessage)
                .where(ChatMessage.conversation_id == conversation_id)
                .order_by(ChatMessage.id)
            ).all()
        )
        assert [row.role for row in rows] == ["user", "assistant", "user", "assistant"]
        submitted_user = rows[-2]
        assert submitted_user.content == '{"체중":"70","식사":"점심"}'
        assert submitted_user.message_type == "input_box"
        assert submitted_user.processing_status == "completed"
        submitted_metadata = json.loads(submitted_user.metadata_json)
        assert submitted_metadata["requested_return_type"] == "input_box"
        assert submitted_metadata["source_chat_request_id"] == source_request_id
        assert rows[-1].content == "입력값을 확인했습니다."
        source_assistant = session.get(ChatMessage, source_assistant_id)
        source_metadata = json.loads(source_assistant.metadata_json)
        assert source_metadata["pending_response_status"] == "answered"
        assert source_metadata["response_message_id"] == submitted_user.public_id


def test_ui_contract_selection_response_preserves_type_and_rejects_mismatch(monkeypatch) -> None:
    suffix = uuid4().hex
    conversation_id = f"ui-selection-conversation-{suffix}"
    source_request_id = f"ui-selection-source-{suffix}"
    captured_thread: dict = {}
    observed_payloads = []

    seed_pending_contract_response(
        conversation_id=conversation_id,
        source_request_id=source_request_id,
        message_type="selection_box",
        message_payload={
            "message_title": "복용 여부",
            "text": "약을 복용했나요?",
            "tables": None,
            "selections": ["복용함", "복용하지 않음"],
            "inputs": None,
        },
    )

    def capture_thread(*, name, target, args):
        captured_thread.update({"name": name, "target": target, "args": args})

    async def fake_send_sync_chat(payload):
        observed_payloads.append(payload)
        return ChatSyncResponse(
            request_id=payload.request_id,
            message_id=payload.message_id,
            message_type="text",
            message=ChatMessageContent(
                message_title=None,
                text="선택을 확인했습니다.",
                tables=None,
                selections=None,
                inputs=None,
            ),
            message_at=NOW,
        )

    monkeypatch.setattr(chat_routes, "start_daemon_thread", capture_thread)
    monkeypatch.setattr(system_main.agent_client, "send_sync_chat", fake_send_sync_chat)
    client = TestClient(system_main.app)
    mismatch = client.post(
        "/chat/contract-response",
        data={
            "conversation_id": conversation_id,
            "source_chat_request_id": source_request_id,
            "response_kind": "input",
            "input_labels": ["복용 여부"],
            "input_values": ["복용함"],
        },
    )
    invalid_selection = client.post(
        "/chat/contract-response",
        data={
            "conversation_id": conversation_id,
            "source_chat_request_id": source_request_id,
            "response_kind": "selection",
            "selection_message": "정의되지 않은 선택지",
        },
    )
    accepted = client.post(
        "/chat/contract-response",
        data={
            "conversation_id": conversation_id,
            "source_chat_request_id": source_request_id,
            "response_kind": "selection",
            "selection_message": "복용함",
        },
    )
    client.close()

    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == "REQUESTED_RETURN_TYPE_MISMATCH"
    assert invalid_selection.status_code == 409
    assert invalid_selection.json()["detail"] == "SELECTION_VALUE_INVALID"
    assert accepted.status_code == 200
    captured_thread["target"](*captured_thread["args"])
    assert len(observed_payloads) == 1
    assert observed_payloads[0].conversation_id == conversation_id
    assert observed_payloads[0].requested_return_type == "selection_box"
    assert observed_payloads[0].message == "복용함"
    with SessionLocal() as session:
        submitted = session.scalar(
            select(ChatMessage)
            .where(
                ChatMessage.conversation_id == conversation_id,
                ChatMessage.role == "user",
                ChatMessage.message_type == "selection_box",
            )
            .order_by(ChatMessage.id.desc())
        )
        assert submitted is not None
        metadata = json.loads(submitted.metadata_json)
        assert metadata["requested_return_type"] == "selection_box"
        assert metadata["source_chat_request_id"] == source_request_id


def test_contract_chat_ui_renders_tables_and_pending_input_controls() -> None:
    suffix = uuid4().hex
    conversation_id = f"ui-render-conversation-{suffix}"
    source_request_id = f"ui-render-source-{suffix}"
    assistant_id = seed_pending_contract_response(
        conversation_id=conversation_id,
        source_request_id=source_request_id,
        message_type="input_box",
        message_payload={
            "message_title": "복약 정보 확인",
            "text": "값을 입력해주세요.",
            "tables": [
                {
                    "table_title": "현재 복약 정보",
                    "rows": [{"column": "복용 시간", "value": "08:00"}],
                }
            ],
            "selections": None,
            "inputs": [
                {
                    "type": "number",
                    "label": "복용량",
                    "value": 1,
                    "options": {
                        "unit": "정",
                        "lower": 0.5,
                        "upper": 3,
                        "selections": None,
                    },
                }
            ],
        },
    )

    client = TestClient(system_main.app)
    pending = client.get("/partials/chat-log")
    assert pending.status_code == 200
    assert "현재 복약 정보" in pending.text
    assert "복용 시간" in pending.text
    assert "08:00" in pending.text
    assert 'name="input_values"' in pending.text
    assert 'step="any"' in pending.text
    assert source_request_id in pending.text

    with SessionLocal() as session:
        assistant = session.get(ChatMessage, assistant_id)
        metadata = json.loads(assistant.metadata_json)
        metadata["pending_response_status"] = "answered"
        assistant.metadata_json = json.dumps(metadata)
        session.commit()

    answered = client.get("/partials/chat-log")
    client.close()
    assert answered.status_code == 200
    assert "이 요청에는 이미 응답했습니다." in answered.text
    assert source_request_id not in answered.text
