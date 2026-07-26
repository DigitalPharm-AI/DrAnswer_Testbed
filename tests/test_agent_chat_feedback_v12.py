from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text

from agent_app import main as agent_main
from agent_app.integration.feedback_contracts import ChatFeedbackRequest
from agent_app.integration.feedback_crypto import (
    FeedbackCipher,
    FeedbackEncryptionError,
)
from agent_app.integration.feedback_service import (
    ACCEPTED,
    FINAL_FAILED,
    PROCESSING,
    RETRYABLE_FAILED,
    ChatFeedbackService,
    feedback_encryption_context,
)
from agent_app.persistence.migrations import run_migrations
from agent_app.persistence.models import AgentFeedbackLink, Base
from agent_app.routes import feedback as feedback_routes
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
)
from shared.db import create_session_factory
from shared.settings import Settings, get_settings
from shared.time_utils import utc_now
from system_app.migrations import run_migrations as run_backend_migrations
from system_app.models import Base as BackendBase
from system_app.models import ChatMessage

FEEDBACK_HEADERS = {"Authorization": "Bearer pytest-agent-sync-token"}
SENSITIVE_FEEDBACK = "절대 로그나 평문 DB에 남으면 안 되는 개인 피드백"


class StubFeedbackBackendQueries:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.not_found = False
        self.role = "assistant"
        self.patient_override = ""
        self.conversation_override = ""

    def validate_feedback_target(
        self,
        *,
        message_id: str,
        conversation_id: str,
        patient_id: str,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "message_id": message_id,
                "conversation_id": conversation_id,
                "patient_id": patient_id,
            }
        )
        if self.not_found:
            raise BackendChatMessageNotFound(
                "backend_chat_message_not_found"
            )
        return {
            "message_id": message_id,
            "conversation_id": (
                self.conversation_override or conversation_id
            ),
            "patient_id": self.patient_override or patient_id,
            "role": self.role,
        }


@pytest.fixture
def feedback_runtime(tmp_path: Path, monkeypatch):
    database_path = (tmp_path / "agent-feedback.db").as_posix()
    engine, sessions = create_session_factory(
        f"sqlite:///{database_path}"
    )
    Base.metadata.create_all(bind=engine)
    run_migrations(engine)
    backend_queries = StubFeedbackBackendQueries()
    monkeypatch.setattr(
        feedback_routes,
        "_feedback_session_factory_getter",
        lambda: sessions,
    )
    monkeypatch.setattr(
        feedback_routes,
        "_feedback_backend_query_tools_getter",
        lambda: backend_queries,
    )
    try:
        yield {
            "database_path": Path(database_path),
            "engine": engine,
            "sessions": sessions,
            "backend": backend_queries,
        }
    finally:
        engine.dispose()


def feedback_payload(
    *,
    request_id: str | None = None,
    feedback_text: str | None = SENSITIVE_FEEDBACK,
) -> dict:
    suffix = uuid4().hex
    return {
        "request_id": request_id or f"feedback-{suffix}",
        "message_id": f"assistant-msg-{suffix}",
        "conversation_id": f"conversation-{suffix}",
        "patient_id": f"patient-{suffix}",
        "feedback": True,
        "feedback_text": feedback_text,
        "feedback_at": "2026-07-25T12:30:00+09:00",
    }


def test_feedback_accepts_202_encrypts_text_and_never_logs_plaintext(
    feedback_runtime,
    caplog,
) -> None:
    payload = feedback_payload()
    caplog.set_level("INFO", logger="uvicorn.error")

    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert len(feedback_runtime["backend"].calls) == 1
    with feedback_runtime["sessions"]() as session:
        row = session.scalar(
            select(AgentFeedbackLink).where(
                AgentFeedbackLink.request_id == payload["request_id"]
            )
        )
        assert row is not None
        assert row.status == ACCEPTED
        assert row.response_status == 202
        assert row.attempt_count == 0
        assert row.max_attempts == 3
        assert row.next_attempt_at is not None
        assert row.expires_at > row.created_at
        assert row.feedback_text_ciphertext.startswith("v1.")
        assert SENSITIVE_FEEDBACK not in row.feedback_text_ciphertext
        assert row.feedback_text_hash != hashlib.sha256(
            SENSITIVE_FEEDBACK.encode("utf-8")
        ).hexdigest()
        assert row.patient_id_hash != hashlib.sha256(
            payload["patient_id"].encode("utf-8")
        ).hexdigest()
        assert row.encryption_key_id == "pytest-feedback-v1"

        request = ChatFeedbackRequest.model_validate(payload)
        cipher = FeedbackCipher.from_settings(get_settings())
        assert row.patient_id_hash == cipher.patient_digest(
            payload["patient_id"]
        )
        context = feedback_encryption_context(
            request,
            patient_id_hash=row.patient_id_hash,
        )
        assert (
            cipher.decrypt(
                row.feedback_text_ciphertext,
                context=context,
            )
            == SENSITIVE_FEEDBACK
        )
        tampered = (
            row.feedback_text_ciphertext[:-1]
            + (
                "A"
                if row.feedback_text_ciphertext[-1] != "A"
                else "B"
            )
        )
        with pytest.raises(FeedbackEncryptionError):
            cipher.decrypt(tampered, context=context)

    assert SENSITIVE_FEEDBACK not in caplog.text
    assert SENSITIVE_FEEDBACK.encode("utf-8") not in feedback_runtime[
        "database_path"
    ].read_bytes()


def test_feedback_same_body_replays_without_duplicate_or_backend_query(
    feedback_runtime,
) -> None:
    payload = feedback_payload()
    client = TestClient(agent_main.app)

    first = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )
    second = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"status": "accepted"}
    assert len(feedback_runtime["backend"].calls) == 1
    with feedback_runtime["sessions"]() as session:
        count = session.scalar(
            select(func.count(AgentFeedbackLink.id)).where(
                AgentFeedbackLink.request_id == payload["request_id"]
            )
        )
    assert count == 1


def test_feedback_same_request_id_with_different_body_conflicts(
    feedback_runtime,
) -> None:
    payload = feedback_payload()
    client = TestClient(agent_main.app)
    assert (
        client.post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=payload,
        ).status_code
        == 202
    )
    payload["feedback_text"] = "다른 본문"

    conflict = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert len(feedback_runtime["backend"].calls) == 1
    with feedback_runtime["sessions"]() as session:
        count = session.scalar(
            select(func.count(AgentFeedbackLink.id)).where(
                AgentFeedbackLink.request_id == payload["request_id"]
            )
        )
    assert count == 1


@pytest.mark.parametrize(
    ("backend_change", "expected_calls"),
    [
        ("not_found", 1),
        ("user_role", 1),
        ("patient_mismatch", 1),
        ("conversation_mismatch", 1),
    ],
)
def test_feedback_rejects_non_ai_or_context_mismatched_target(
    feedback_runtime,
    backend_change: str,
    expected_calls: int,
) -> None:
    backend = feedback_runtime["backend"]
    if backend_change == "not_found":
        backend.not_found = True
    elif backend_change == "user_role":
        backend.role = "user"
    elif backend_change == "patient_mismatch":
        backend.patient_override = "another-patient"
    else:
        backend.conversation_override = "another-conversation"

    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=feedback_payload(),
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "BACKEND_MESSAGE_NOT_FOUND"
    assert len(backend.calls) == expected_calls
    with feedback_runtime["sessions"]() as session:
        assert (
            session.scalar(select(func.count(AgentFeedbackLink.id)))
            == 0
        )


def test_feedback_target_is_verified_through_real_backend_read_views(
    feedback_runtime,
    monkeypatch,
) -> None:
    backend_path = (
        feedback_runtime["database_path"].parent
        / "backend-feedback-target.db"
    ).as_posix()
    backend_engine, backend_sessions = create_session_factory(
        f"sqlite:///{backend_path}"
    )
    BackendBase.metadata.create_all(bind=backend_engine)
    run_backend_migrations(backend_engine)
    assistant_id = "assistant_msg_feedback_target"
    user_id = "user_msg_feedback_target"
    conversation_id = "conversation-feedback-target"
    patient_id = "patient-feedback-target"
    with backend_sessions() as session:
        session.add_all(
            [
                ChatMessage(
                    public_id=assistant_id,
                    patient_id=patient_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    sender_type="assistant",
                    content="검증할 AI 답변",
                ),
                ChatMessage(
                    public_id=user_id,
                    patient_id=patient_id,
                    conversation_id=conversation_id,
                    role="user",
                    sender_type="user",
                    content="피드백 대상이 될 수 없는 사용자 메시지",
                ),
            ]
        )
        session.commit()
    backend_queries = BackendQueryTools(f"sqlite:///{backend_path}")
    monkeypatch.setattr(
        feedback_routes,
        "_feedback_backend_query_tools_getter",
        lambda: backend_queries,
    )
    try:
        valid = feedback_payload()
        valid.update(
            {
                "message_id": assistant_id,
                "conversation_id": conversation_id,
                "patient_id": patient_id,
            }
        )
        accepted = TestClient(agent_main.app).post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=valid,
        )
        assert accepted.status_code == 202

        invalid = feedback_payload()
        invalid.update(
            {
                "message_id": user_id,
                "conversation_id": conversation_id,
                "patient_id": patient_id,
            }
        )
        rejected = TestClient(agent_main.app).post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=invalid,
        )
        assert rejected.status_code == 404
        assert (
            rejected.json()["error"]["code"]
            == "BACKEND_MESSAGE_NOT_FOUND"
        )
    finally:
        backend_queries.engine.dispose()
        backend_engine.dispose()


def test_feedback_requires_agent_bearer_and_aware_datetime(
    feedback_runtime,
) -> None:
    client = TestClient(agent_main.app)
    payload = feedback_payload()

    unauthorized = client.post(
        "/agent/async/chat_feedback",
        json=payload,
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    assert feedback_runtime["backend"].calls == []

    payload["feedback_at"] = "2026-07-25T12:30:00"
    invalid = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert feedback_runtime["backend"].calls == []

    missing_text_field = feedback_payload()
    del missing_text_field["feedback_text"]
    missing = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=missing_text_field,
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "INVALID_REQUEST"
    assert feedback_runtime["backend"].calls == []


def test_feedback_fails_closed_without_backend_read_tools(
    feedback_runtime,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feedback_routes,
        "_feedback_backend_query_tools_getter",
        lambda: None,
    )

    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=feedback_payload(),
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BACKEND_DB_UNAVAILABLE"
    with feedback_runtime["sessions"]() as session:
        assert (
            session.scalar(select(func.count(AgentFeedbackLink.id)))
            == 0
        )


def test_feedback_route_fails_closed_when_encryption_is_unavailable(
    feedback_runtime,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        feedback_routes,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            agent_feedback_encryption_key="",
            agent_sync_api_token="pytest-agent-sync-token",
            backend_api_token="pytest-backend-api-token",
        ),
    )

    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=feedback_payload(),
    )

    assert response.status_code == 503
    assert (
        response.json()["error"]["code"]
        == "FEEDBACK_ENCRYPTION_UNAVAILABLE"
    )
    assert feedback_runtime["backend"].calls == []
    with feedback_runtime["sessions"]() as session:
        assert (
            session.scalar(select(func.count(AgentFeedbackLink.id)))
            == 0
        )


def test_feedback_retry_policy_reaches_terminal_failure(
    feedback_runtime,
) -> None:
    payload = feedback_payload(feedback_text=None)
    client = TestClient(agent_main.app)
    assert (
        client.post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=payload,
        ).status_code
        == 202
    )
    service = ChatFeedbackService(
        feedback_runtime["sessions"],
        settings=get_settings(),
    )
    current = utc_now() + timedelta(seconds=1)

    for expected_attempt in (1, 2, 3):
        claimed = service.claim_due(now=current)
        assert len(claimed) == 1
        assert claimed[0].attempt_count == expected_attempt
        with feedback_runtime["sessions"]() as session:
            row = session.get(AgentFeedbackLink, claimed[0].record_id)
            assert row is not None
            assert row.status == PROCESSING
        service.fail(
            claimed[0].record_id,
            error_code="DOWNSTREAM_TEMPORARY",
            retryable=True,
            now=current,
        )
        with feedback_runtime["sessions"]() as session:
            row = session.get(AgentFeedbackLink, claimed[0].record_id)
            assert row is not None
            if expected_attempt < 3:
                assert row.status == RETRYABLE_FAILED
                assert row.next_attempt_at is not None
                assert service.claim_due(now=current) == []
                current = row.next_attempt_at
            else:
                assert row.status == FINAL_FAILED
                assert row.next_attempt_at is None
                assert row.processed_at == current
    assert service.claim_due(now=current + timedelta(days=1)) == []


def test_feedback_processing_lease_recovers_crashes_and_stops_at_max_attempts(
    feedback_runtime,
) -> None:
    payload = feedback_payload(feedback_text=None)
    assert (
        TestClient(agent_main.app).post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=payload,
        ).status_code
        == 202
    )
    service = ChatFeedbackService(
        feedback_runtime["sessions"],
        settings=get_settings(),
    )
    current = utc_now() + timedelta(seconds=1)
    record_id = 0

    for expected_attempt in (1, 2, 3):
        claimed = service.claim_due(now=current)
        assert len(claimed) == 1
        record_id = claimed[0].record_id
        assert claimed[0].attempt_count == expected_attempt
        assert (
            service.claim_due(
                now=current
                + timedelta(
                    seconds=service.processing_lease_seconds - 1
                )
            )
            == []
        )
        current += timedelta(
            seconds=service.processing_lease_seconds
        )

    assert service.claim_due(now=current) == []
    with feedback_runtime["sessions"]() as session:
        row = session.get(AgentFeedbackLink, record_id)
        assert row is not None
        assert row.status == FINAL_FAILED
        assert row.error_code == "PROCESSING_LEASE_EXPIRED"
        assert row.processed_at == current
        assert row.next_attempt_at is None


def test_feedback_migration_adds_idempotency_and_retry_columns(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "feedback-migration.db").as_posix()
    engine, _ = create_session_factory(f"sqlite:///{database_path}")
    try:
        applied = run_migrations(engine)
        columns = {
            column["name"]: column
            for column in inspect(engine).get_columns(
                "agent_feedback_links"
            )
        }
        assert {
            "request_hash",
            "response_status",
            "response_json",
            "attempt_count",
            "max_attempts",
            "next_attempt_at",
            "last_attempt_at",
        } <= set(columns)
        assert columns["feedback"]["nullable"] is False
        assert "20260725_0025_agent_feedback_processing_policy" in applied
        assert "ix_agent_feedback_links_next_attempt_at" in {
            index["name"]
            for index in inspect(engine).get_indexes(
                "agent_feedback_links"
            )
        }
    finally:
        engine.dispose()


def test_feedback_migration_upgrades_prepared_legacy_table(
    tmp_path: Path,
) -> None:
    database_path = (tmp_path / "legacy-feedback.db").as_posix()
    engine, _ = create_session_factory(f"sqlite:///{database_path}")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE agent_feedback_links (
                        id INTEGER PRIMARY KEY,
                        api_path VARCHAR(160) NOT NULL,
                        request_id VARCHAR(160) NOT NULL,
                        message_id VARCHAR(160) NOT NULL,
                        conversation_id VARCHAR(160) NOT NULL,
                        patient_id_hash VARCHAR(64) NOT NULL,
                        trace_id VARCHAR(160) NOT NULL DEFAULT '',
                        feedback BOOLEAN,
                        feedback_text_ciphertext TEXT NOT NULL DEFAULT '',
                        feedback_text_hash VARCHAR(64) NOT NULL DEFAULT '',
                        encryption_key_id VARCHAR(120) NOT NULL DEFAULT '',
                        status VARCHAR(32) NOT NULL DEFAULT 'ACCEPTED',
                        error_code VARCHAR(80) NOT NULL DEFAULT '',
                        feedback_at DATETIME NOT NULL,
                        processed_at DATETIME,
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL,
                        expires_at DATETIME NOT NULL,
                        UNIQUE (api_path, request_id)
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO agent_feedback_links (
                        id, api_path, request_id, message_id,
                        conversation_id, patient_id_hash, feedback,
                        feedback_at, created_at, updated_at, expires_at
                    ) VALUES (
                        1, '/agent/async/chat_feedback', 'legacy-request',
                        'assistant-msg-legacy', 'conversation-legacy',
                        'patient-hmac', NULL, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP, CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        run_migrations(engine)

        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT feedback, response_status, response_json,
                           attempt_count, max_attempts, next_attempt_at
                    FROM agent_feedback_links
                    WHERE request_id = 'legacy-request'
                    """
                )
            ).mappings().one()
        assert bool(row["feedback"]) is False
        assert row["response_status"] == 202
        assert row["response_json"] == '{"status":"accepted"}'
        assert row["attempt_count"] == 0
        assert row["max_attempts"] == 3
        assert row["next_attempt_at"] is not None
    finally:
        engine.dispose()


def test_feedback_encryption_configuration_is_fail_closed() -> None:
    with pytest.raises(
        RuntimeError,
        match="AGENT_FEEDBACK_ENCRYPTION_KEY",
    ):
        Settings(
            agent_feedback_encryption_key="",
        ).require_agent_feedback_encryption()

    with pytest.raises(
        RuntimeError,
        match="exactly 32 bytes",
    ):
        Settings(
            agent_feedback_encryption_key="c2hvcnQ=",
        ).require_agent_feedback_encryption()
