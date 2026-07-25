from __future__ import annotations

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
from system_app import main as system_main
from system_app.routes import chat as chat_routes
from system_app.db import SessionLocal
from system_app.migrations import run_migrations
from system_app.models import Base, ChatMessage, DoseEvent, ReminderPolicy

NOW = datetime(2026, 7, 25, 13, 0, tzinfo=UTC)


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
            persisted = session.get(ChatMessage, int(payload.message_id))
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

    assert first.status_code == 200
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


def test_backend_record_change_enforces_confirmation_version_and_idempotency() -> None:
    suffix = uuid4().hex
    patient_id = f"patient-{suffix}"
    conversation_id = f"conversation-{suffix}"
    source_request_id = f"source-{suffix}"
    write_request_id = f"write-{suffix}"
    schedule_id = 1_000_000 + int(suffix[:7], 16)
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
        confirmation = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
            ai_request_id=f"confirm-{suffix}",
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="변경 적용",
            processing_status="completed",
            created_at=NOW.replace(tzinfo=None),
        )
        session.add(confirmation)
        event = DoseEvent(
            patient_id=patient_id,
            plan_id=schedule_id,
            schedule_id=schedule_id,
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
        confirmation_id = confirmation.id
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
    first = client.post("/agent/sync/record-change", json=body)
    replay = client.post("/agent/sync/record-change", json=body)
    changed = {**body, "payload": {**body["payload"], "reason": "다른 본문"}}
    conflict = client.post("/agent/sync/record-change", json=changed)
    client.close()

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
        confirmation = ChatMessage(
            patient_id=patient_id,
            conversation_id=conversation_id,
            ai_request_id=f"confirm-policy-{suffix}",
            role="user",
            sender_type="patient",
            category="multiturn_chat",
            message_type="text",
            content="그대로 적용해줘.",
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
        session.add_all([source, confirmation, policy])
        session.commit()
        confirmation_id = confirmation.id
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
    applied = client.post("/agent/sync/notification-policy-change", json=body)
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
    assert context["recent_chat"][0]["message_id"] == str(message_id)
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
        assert [row.role for row in rows] == ["user", "assistant"]
        assert rows[0].content == '{"체중":"70","식사":"점심"}'
        assert rows[0].processing_status == "completed"
        assert rows[1].content == "입력값을 확인했습니다."
