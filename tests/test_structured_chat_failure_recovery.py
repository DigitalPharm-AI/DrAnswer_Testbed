from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from shared.chat_contracts import ChatMessageContent, ChatSyncResponse
from shared.json_utils import dump_json, parse_json_object
from system_app.contracts_v13 import BackendChatRequest
from system_app.models import ChatMessage
from system_app.services.backend_chat_service import (
    mark_user_message_failed,
    persist_assistant_response,
    persist_user_message,
)
from sqlalchemy.orm import sessionmaker
from tests.helpers import build_system_engine


def _sessions(tmp_path):
    engine, _cleanup = build_system_engine(
        "structured_chat_recovery"
    )
    return sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )


def _seed_pending_card(session) -> ChatMessage:
    source = ChatMessage(
        public_id="assistant_msg_0000000000000001",
        patient_id="patient_0000000000000001",
        ai_request_id="req_0000000000000001",
        role="assistant",
        sender_type="assistant",
        message_type="selection_box",
        content="변경할까요?",
        message_payload_json=dump_json(
            {
                "message_title": "복약 기록 변경",
                "text": "변경할까요?",
                "tables": None,
                "selections": ["변경 적용", "취소"],
                "inputs": None,
            }
        ),
        processing_status="completed",
        metadata_json=dump_json(
            {
                "pending_response_type": "selection_box",
                "pending_response_status": "pending",
            }
        ),
    )
    session.add(source)
    session.flush()
    return source


def test_failed_structured_submission_reopens_source_card(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    with sessions() as session:
        source = _seed_pending_card(session)
        user, request_id, _message_at = persist_user_message(
            session,
            BackendChatRequest(
                patient_id="patient_0000000000000001",
                message="변경 적용",
                requested_return_type="selection_box",
                source_message_id=source.public_id,
                request_id="req_0000000000000002",
                message_at=datetime(2026, 7, 26, 9, 0, tzinfo=UTC),
            ),
        )
        session.commit()

        assert request_id == "req_0000000000000002"
        assert (
            parse_json_object(source.metadata_json)[
                "pending_response_status"
            ]
            == "answered"
        )

        mark_user_message_failed(
            session,
            user_message_id=user.id,
            error_code="AGENT_UNAVAILABLE",
            retryable=True,
        )
        session.commit()

        refreshed_source = session.scalar(
            select(ChatMessage).where(
                ChatMessage.public_id == "assistant_msg_0000000000000001"
            )
        )
        metadata = parse_json_object(refreshed_source.metadata_json)
        assert metadata["pending_response_status"] == "pending"
        assert "response_message_id" not in metadata
        assert "response_request_id" not in metadata
        assert user.processing_status == "retryable_failed"


def test_completed_structured_submission_is_not_reopened(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    with sessions() as session:
        source = _seed_pending_card(session)
        user, _request_id, _message_at = persist_user_message(
            session,
            BackendChatRequest(
                patient_id="patient_0000000000000001",
                message="변경 적용",
                requested_return_type="selection_box",
                source_message_id=source.public_id,
                request_id="req_0000000000000003",
                message_at=datetime(2026, 7, 26, 9, 0, tzinfo=UTC),
            ),
        )
        persist_assistant_response(
            session,
            user_message=user,
            response=ChatSyncResponse(
                request_id="req_0000000000000003",
                message_id=user.public_id,
                message_type="text",
                message=ChatMessageContent(
                    message_title=None,
                    text="변경했습니다.",
                    tables=None,
                    selections=None,
                    inputs=None,
                ),
                message_at=datetime(2026, 7, 26, 9, 1, tzinfo=UTC),
            ),
        )
        mark_user_message_failed(
            session,
            user_message_id=user.id,
            error_code="LATE_FAILURE",
            retryable=True,
        )
        session.commit()

        metadata = parse_json_object(source.metadata_json)
        assert metadata["pending_response_status"] == "answered"
        assert metadata["response_message_id"] == user.public_id


def test_new_free_text_supersedes_pending_structured_card(tmp_path) -> None:
    sessions = _sessions(tmp_path)
    with sessions() as session:
        source = _seed_pending_card(session)

        user, request_id, _message_at = persist_user_message(
            session,
            BackendChatRequest(
                patient_id="patient_0000000000000001",
                message="새로운 증상 질문이 있어요.",
                requested_return_type="text",
                source_message_id=None,
                request_id="req_0000000000000004",
                message_at=datetime(2026, 7, 26, 9, 2, tzinfo=UTC),
            ),
        )
        session.commit()

        assert request_id == "req_0000000000000004"
        assert user.message_type == "text"
        assert user.reply_to_message_id is None
        assert user.content == "새로운 증상 질문이 있어요."
        assert parse_json_object(source.metadata_json)[
            "pending_response_status"
        ] == "superseded"
        assert "source_chat_message_id" not in parse_json_object(
            user.metadata_json
        )
