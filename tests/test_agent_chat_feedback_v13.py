from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import sessionmaker

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
from agent_app.persistence.models import AgentFeedbackLink
from agent_app.routes import feedback as feedback_routes
from agent_app.tools.backend_query import (
    BackendChatMessageNotFound,
    BackendQueryTools,
)
from shared.settings import Settings, get_settings
from shared.time_utils import utc_now
from system_app.migrations import run_migrations as run_backend_migrations
from system_app.models import ChatMessage
from tests.helpers import (
    build_agent_engine,
    build_backend_reader_url,
    build_system_engine,
)

FEEDBACK_HEADERS = {"Authorization": "Bearer pytest-agent-sync-token"}
SENSITIVE_FEEDBACK = "절대 로그나 평문 DB에 남으면 안 되는 개인 피드백"


class StubFeedbackBackendQueries:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.not_found = False
        self.role = "assistant"
        self.patient_override = ""

    def validate_feedback_target(
        self,
        *,
        message_id: str,
        patient_id: str,
    ) -> dict[str, str]:
        self.calls.append(
            {
                "message_id": message_id,
                "patient_id": patient_id,
            }
        )
        if self.not_found:
            raise BackendChatMessageNotFound(
                "backend_chat_message_not_found"
            )
        return {
            "message_id": message_id,
            "patient_id": self.patient_override or patient_id,
            "role": self.role,
        }


@pytest.fixture
def feedback_runtime(tmp_path: Path, monkeypatch):
    engine, cleanup = build_agent_engine("agent_feedback")
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
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
            "engine": engine,
            "sessions": sessions,
            "backend": backend_queries,
        }
    finally:
        cleanup()


def feedback_payload(
    *,
    request_id: str | None = None,
    feedback_text: str | None = SENSITIVE_FEEDBACK,
    reaction: str | None = None,
) -> dict:
    suffix = uuid4().hex[:16]
    payload = {
        "request_id": request_id or f"req_{suffix}",
        "message_id": f"assistant_msg_{suffix}",
        "patient_id": f"patient_{suffix}",
        "feedback_text": feedback_text,
        "feedback_at": "2026-07-25T12:30:00+09:00",
    }
    if reaction is not None:
        payload["reaction"] = reaction
    return payload


def assert_error_envelope(
    response,
    *,
    request_id: str | None,
    code: str,
    message: str | None = None,
) -> dict:
    body = response.json()
    assert set(body) == {"request_id", "error"}
    assert body["request_id"] == request_id
    assert set(body["error"]) == {
        "code",
        "message",
        "retryable",
        "details",
    }
    assert body["error"]["code"] == code
    if message is not None:
        assert body["error"]["message"] == message
    return body


def assert_accepted_response(
    response,
    *,
    reaction: str | None,
) -> dict:
    body = response.json()
    assert body["status"] == "accepted"
    assert body["reaction"] == reaction
    accepted_at = datetime.fromisoformat(body["accepted_at"])
    assert accepted_at.tzinfo is not None
    assert accepted_at.utcoffset() is not None
    return body


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
    assert_accepted_response(response, reaction=None)
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
        assert row.feedback is None
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
    assert SENSITIVE_FEEDBACK not in row.feedback_text_ciphertext
    assert SENSITIVE_FEEDBACK not in row.feedback_text_hash


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
    assert first.json() == second.json()
    assert_accepted_response(first, reaction=None)
    assert len(feedback_runtime["backend"].calls) == 1
    with feedback_runtime["sessions"]() as session:
        count = session.scalar(
            select(func.count(AgentFeedbackLink.id)).where(
                AgentFeedbackLink.request_id == payload["request_id"]
            )
        )
    assert count == 1


def test_feedback_reaction_switch_is_mutually_exclusive_and_old_replay_is_exact(
    feedback_runtime,
) -> None:
    client = TestClient(agent_main.app)
    like = feedback_payload(feedback_text=None, reaction="like")

    liked = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=like,
    )
    assert liked.status_code == 202
    liked_body = assert_accepted_response(liked, reaction="like")

    dislike = {
        **like,
        "request_id": "req_0000000000000002",
        "reaction": "dislike",
        "feedback_at": "2026-07-25T12:31:00+09:00",
    }
    disliked = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=dislike,
    )
    assert disliked.status_code == 202
    disliked_body = assert_accepted_response(
        disliked,
        reaction="dislike",
    )
    assert disliked_body["accepted_at"] >= liked_body["accepted_at"]

    old_replay = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=like,
    )
    assert old_replay.status_code == 202
    assert old_replay.json() == liked_body

    opinion = {
        **like,
        "request_id": "req_0000000000000003",
        "feedback_text": SENSITIVE_FEEDBACK,
        "feedback_at": "2026-07-25T12:32:00+09:00",
    }
    opinion.pop("reaction")
    opinion_response = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=opinion,
    )
    assert opinion_response.status_code == 202
    assert_accepted_response(opinion_response, reaction=None)

    with feedback_runtime["sessions"]() as session:
        rows = list(
            session.scalars(
                select(AgentFeedbackLink)
                .where(
                    AgentFeedbackLink.message_id
                    == like["message_id"]
                )
                .order_by(AgentFeedbackLink.id)
            ).all()
        )
        active = [row for row in rows if row.feedback is not None]
        assert len(rows) == 3
        assert len(active) == 1
        assert active[0].request_id == dislike["request_id"]
        assert active[0].feedback is False
        opinion_row = next(
            row
            for row in rows
            if row.request_id == opinion["request_id"]
        )
        assert opinion_row.feedback is None
        assert opinion_row.feedback_text_ciphertext.startswith("v1.")
        assert SENSITIVE_FEEDBACK not in opinion_row.feedback_text_ciphertext

    conflicting_like = {**like, "reaction": "dislike"}
    conflict = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=conflicting_like,
    )
    assert conflict.status_code == 409
    assert_error_envelope(
        conflict,
        request_id=like["request_id"],
        code="IDEMPOTENCY_CONFLICT",
    )


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
    assert_error_envelope(
        conflict,
        request_id=payload["request_id"],
        code="IDEMPOTENCY_CONFLICT",
    )
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
        backend.patient_override = "patient_00000000deadbeef"

    payload = feedback_payload()
    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert response.status_code == 404
    assert_error_envelope(
        response,
        request_id=payload["request_id"],
        code="BACKEND_MESSAGE_NOT_FOUND",
        message="The verified AI answer message was not found.",
    )
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
    backend_engine, backend_cleanup = build_system_engine(
        "feedback_backend_target"
    )
    backend_sessions = sessionmaker(
        bind=backend_engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    run_backend_migrations(backend_engine)
    assistant_id = "assistant_msg_0000000000000001"
    user_id = "user_msg_0000000000000001"
    patient_id = "patient_0000000000000001"
    with backend_sessions() as session:
        session.add_all(
            [
                ChatMessage(
                    public_id=assistant_id,
                    patient_id=patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content="검증할 AI 답변",
                ),
                ChatMessage(
                    public_id=user_id,
                    patient_id=patient_id,
                    role="user",
                    sender_type="user",
                    content="피드백 대상이 될 수 없는 사용자 메시지",
                ),
            ]
        )
        session.commit()
    reader_url, reader_cleanup = build_backend_reader_url(
        backend_engine,
        "feedback_backend_reader",
    )
    backend_queries = BackendQueryTools(reader_url)
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
                "message_id": assistant_id,
                "patient_id": "patient_0000000000000002",
            }
        )
        rejected = TestClient(agent_main.app).post(
            "/agent/async/chat_feedback",
            headers=FEEDBACK_HEADERS,
            json=invalid,
        )
        assert rejected.status_code == 404
        assert_error_envelope(
            rejected,
            request_id=invalid["request_id"],
            code="BACKEND_MESSAGE_NOT_FOUND",
            message="The verified AI answer message was not found.",
        )
    finally:
        backend_queries.engine.dispose()
        reader_cleanup()
        backend_cleanup()


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
    assert_error_envelope(
        unauthorized,
        request_id=payload["request_id"],
        code="UNAUTHORIZED",
    )
    assert feedback_runtime["backend"].calls == []

    payload["feedback_at"] = "2026-07-25T12:30:00"
    invalid = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )
    assert invalid.status_code == 400
    assert_error_envelope(
        invalid,
        request_id=payload["request_id"],
        code="INVALID_REQUEST",
    )
    assert feedback_runtime["backend"].calls == []

    missing_text_field = feedback_payload()
    del missing_text_field["feedback_text"]
    missing = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=missing_text_field,
    )
    assert missing.status_code == 400
    assert_error_envelope(
        missing,
        request_id=missing_text_field["request_id"],
        code="INVALID_REQUEST",
    )
    assert feedback_runtime["backend"].calls == []

    reaction_only = feedback_payload(
        feedback_text=None,
        reaction="like",
    )
    accepted_reaction = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=reaction_only,
    )
    assert accepted_reaction.status_code == 202
    assert_accepted_response(accepted_reaction, reaction="like")
    feedback_runtime["backend"].calls.clear()

    legacy_rating = feedback_payload()
    legacy_rating["feedback"] = True
    rejected_rating = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=legacy_rating,
    )
    assert rejected_rating.status_code == 400
    assert_error_envelope(
        rejected_rating,
        request_id=legacy_rating["request_id"],
        code="INVALID_REQUEST",
    )
    assert feedback_runtime["backend"].calls == []

    blank_text = feedback_payload(feedback_text="   ")
    rejected_blank = client.post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=blank_text,
    )
    assert rejected_blank.status_code == 400
    assert_error_envelope(
        rejected_blank,
        request_id=blank_text["request_id"],
        code="INVALID_REQUEST",
    )
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

    payload = feedback_payload()
    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert response.status_code == 503
    assert_error_envelope(
        response,
        request_id=payload["request_id"],
        code="BACKEND_DB_UNAVAILABLE",
        message=(
            "The read-only Backend DB connection is unavailable or "
            "incompatible."
        ),
    )
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

    payload = feedback_payload()
    response = TestClient(agent_main.app).post(
        "/agent/async/chat_feedback",
        headers=FEEDBACK_HEADERS,
        json=payload,
    )

    assert response.status_code == 503
    assert_error_envelope(
        response,
        request_id=payload["request_id"],
        code="FEEDBACK_ENCRYPTION_UNAVAILABLE",
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
    payload = feedback_payload(feedback_text="retry processing opinion")
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
    payload = feedback_payload(feedback_text="lease recovery opinion")
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
    engine, cleanup = build_agent_engine(
        "feedback_migration",
        create_models=False,
    )
    try:
        run_migrations(engine)
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
        assert columns["feedback"]["nullable"] is True
        indexes = {
            index["name"]
            for index in inspect(engine).get_indexes(
                "agent_feedback_links"
            )
        }
        assert "ix_agent_feedback_links_next_attempt_at" in indexes
        assert "uq_agent_feedback_links_current_reaction" in indexes
    finally:
        cleanup()




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
