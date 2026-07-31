from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from shared.settings import get_settings
from system_app import main as system_main
from system_app.db import get_session
from system_app.models import ChatMessage
from system_app.routes.ui_feedback import (
    UI_CHAT_FEEDBACK_PATH,
    create_ui_feedback_router,
)
from system_app.services import agent_client as agent_client_module
from system_app.services.agent_client import AgentClient
from system_app.services.agent_client import AgentServiceError
from system_app.services.ui_feedback_service import feedback_status_from_metadata
from tests.helpers import build_system_engine


class StubAgentFeedbackClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.error: AgentServiceError | None = None
        self.result: dict = {"status": "accepted"}

    async def send_chat_feedback(self, payload: dict) -> dict:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return dict(self.result)


def _app_with_database(
    tmp_path: Path,
) -> tuple[TestClient, StubAgentFeedbackClient, object]:
    engine, _cleanup = build_system_engine(
        "ui_feedback"
    )
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    agent_client = StubAgentFeedbackClient()
    runtime = SimpleNamespace(
        agent_client=agent_client,
        write_lock=threading.RLock(),
    )
    app = FastAPI()
    app.include_router(create_ui_feedback_router(lambda: runtime))

    def session_override():
        with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    return TestClient(app), agent_client, sessions


def _payload() -> dict:
    return {
        "request_id": "req_0000000000000001",
        "assistant_message_id": "assistant_msg_0000000000000001",
        "opinion_text": "답변에 복약 시간 설명을 조금 더 추가해 주세요.",
        "feedback_at": datetime(2026, 7, 26, 9, 30, tzinfo=UTC).isoformat(),
    }


def test_ui_feedback_derives_trusted_context_and_hides_it_from_browser(
    tmp_path: Path,
) -> None:
    client, agent_client, sessions = _app_with_database(tmp_path)
    with sessions() as session:
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000000001",
                patient_id=get_settings().patient_id,
                role="assistant",
                sender_type="assistant",
                content="AI 답변",
            )
        )
        session.commit()

    response = client.post(UI_CHAT_FEEDBACK_PATH, json=_payload())

    assert response.status_code == 202
    body = response.json()
    assert body["success"] is True
    assert body["error"] is None
    assert body["data"]["status"] == "accepted"
    assert body["data"]["request_id"] == "req_0000000000000001"
    assert (
        body["data"]["assistant_message_id"]
        == "assistant_msg_0000000000000001"
    )
    assert body["data"]["reaction"] is None
    assert body["data"]["opinion_submitted"] is True
    response_submitted_at = datetime.fromisoformat(
        body["data"]["opinion_submitted_at"]
    )
    assert response_submitted_at.utcoffset() == datetime.now(UTC).utcoffset()
    assert agent_client.calls == [
        {
            "request_id": "req_0000000000000001",
            "message_id": "assistant_msg_0000000000000001",
            "patient_id": get_settings().patient_id,
            "reaction": None,
            "feedback_text": "답변에 복약 시간 설명을 조금 더 추가해 주세요.",
            "feedback_at": "2026-07-26T09:30:00+00:00",
        }
    ]
    assert "patient_id" not in response.text
    assert "conversation_id" not in response.text
    with sessions() as session:
        message = session.query(ChatMessage).filter_by(
            public_id="assistant_msg_0000000000000001"
        ).one()
        metadata = json.loads(message.metadata_json)
        assert metadata["opinion_submitted"] is True
        assert metadata["opinion_request_id"] == "req_0000000000000001"
        metadata_submitted_at = datetime.fromisoformat(
            metadata["opinion_submitted_at"]
        )
        assert (
            metadata_submitted_at.utcoffset()
            == datetime.now(UTC).utcoffset()
        )
        assert "opinion_text" not in metadata
        assert "답변에 복약" not in message.metadata_json
        feedback_status = feedback_status_from_metadata(message.metadata_json)
        assert feedback_status["opinion_submitted"] is True
        assert (
            feedback_status["opinion_submitted_at"]
            == metadata["opinion_submitted_at"]
        )
        first_submitted_at = metadata["opinion_submitted_at"]

    replay = client.post(UI_CHAT_FEEDBACK_PATH, json=_payload())
    assert replay.status_code == 202
    with sessions() as session:
        replayed_message = session.query(ChatMessage).filter_by(
            public_id="assistant_msg_0000000000000001"
        ).one()
        replayed_metadata = json.loads(replayed_message.metadata_json)
        assert (
            replayed_metadata["opinion_submitted_at"]
            == first_submitted_at
        )


def test_ui_feedback_reaction_switch_ignores_older_replay_and_opinion_only(
    tmp_path: Path,
) -> None:
    client, agent_client, sessions = _app_with_database(tmp_path)
    with sessions() as session:
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000000001",
                patient_id=get_settings().patient_id,
                role="assistant",
                sender_type="assistant",
                content="AI 답변",
            )
        )
        session.commit()

    like_payload = {
        "request_id": "req_0000000000000002",
        "assistant_message_id": "assistant_msg_0000000000000001",
        "reaction": "like",
        "feedback_at": "2026-07-26T09:30:00+00:00",
    }
    agent_client.result = {
        "status": "accepted",
        "reaction": "like",
        "accepted_at": "2026-07-26T09:30:01+00:00",
    }
    liked = client.post(UI_CHAT_FEEDBACK_PATH, json=like_payload)
    assert liked.status_code == 202
    assert liked.json()["data"]["reaction"] == "like"
    assert liked.json()["data"]["opinion_submitted"] is False

    dislike_payload = {
        **like_payload,
        "request_id": "req_0000000000000003",
        "reaction": "dislike",
        "feedback_at": "2026-07-26T09:31:00+00:00",
    }
    agent_client.result = {
        "status": "accepted",
        "reaction": "dislike",
        "accepted_at": "2026-07-26T09:31:01+00:00",
    }
    disliked = client.post(
        UI_CHAT_FEEDBACK_PATH,
        json=dislike_payload,
    )
    assert disliked.status_code == 202
    assert disliked.json()["data"]["reaction"] == "dislike"

    # Agent replays the first response verbatim. Backend metadata must retain
    # the newer dislike because accepted_at is older.
    agent_client.result = {
        "status": "accepted",
        "reaction": "like",
        "accepted_at": "2026-07-26T09:30:01+00:00",
    }
    old_replay = client.post(
        UI_CHAT_FEEDBACK_PATH,
        json=like_payload,
    )
    assert old_replay.status_code == 202
    assert old_replay.json()["data"]["reaction"] == "dislike"

    # Even a later opinion acceptance carrying a reaction value cannot update
    # reaction metadata because the browser request was opinion-only.
    agent_client.result = {
        "status": "accepted",
        "reaction": "like",
        "accepted_at": "2026-07-26T09:32:01+00:00",
    }
    opinion_payload = {
        "request_id": "req_0000000000000004",
        "assistant_message_id": "assistant_msg_0000000000000001",
        "opinion_text": "설명을 더 짧게 보여주세요.",
        "feedback_at": "2026-07-26T09:32:00+00:00",
    }
    opinion = client.post(
        UI_CHAT_FEEDBACK_PATH,
        json=opinion_payload,
    )
    assert opinion.status_code == 202
    assert opinion.json()["data"]["reaction"] == "dislike"
    assert opinion.json()["data"]["opinion_submitted"] is True

    with sessions() as session:
        message = session.query(ChatMessage).filter_by(
            public_id="assistant_msg_0000000000000001"
        ).one()
        metadata = json.loads(message.metadata_json)
        assert feedback_status_from_metadata(message.metadata_json) == {
            "reaction": "dislike",
            "opinion_submitted": True,
            "opinion_submitted_at": "2026-07-26T09:32:01+00:00",
        }
        assert (
            metadata["feedback_reaction_request_id"]
            == "req_0000000000000003"
        )
        assert (
            metadata["feedback_reaction_accepted_at"]
            == "2026-07-26T09:31:01+00:00"
        )
        assert "opinion_text" not in metadata
        assert "설명을 더 짧게" not in message.metadata_json

    assert all(
        "patient_id" not in payload
        for payload in (
            like_payload,
            dislike_payload,
            opinion_payload,
        )
    )
    assert all(
        call["patient_id"] == get_settings().patient_id
        for call in agent_client.calls
    )


def test_ui_feedback_reaction_requires_agent_acceptance_time(
    tmp_path: Path,
) -> None:
    client, agent_client, sessions = _app_with_database(tmp_path)
    with sessions() as session:
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000000001",
                patient_id=get_settings().patient_id,
                role="assistant",
                sender_type="assistant",
                content="AI 답변",
            )
        )
        session.commit()
    agent_client.result = {
        "status": "accepted",
        "reaction": "like",
    }

    response = client.post(
        UI_CHAT_FEEDBACK_PATH,
        json={
            "request_id": "req_0000000000000005",
            "assistant_message_id": "assistant_msg_0000000000000001",
            "reaction": "like",
            "feedback_at": "2026-07-26T09:30:00+00:00",
        },
    )

    assert response.status_code == 502
    assert (
        response.json()["error"]["code"]
        == "AGENT_FEEDBACK_RESPONSE_INVALID"
    )


def test_ui_feedback_rejects_non_assistant_or_other_patient_and_ignores_legacy_thread_key(
    tmp_path: Path,
) -> None:
    client, agent_client, sessions = _app_with_database(tmp_path)
    with sessions() as session:
        session.add_all(
            [
                ChatMessage(
                    public_id="assistant_msg_0000000000000001",
                    patient_id="patient_0000000000000002",
                    role="assistant",
                    sender_type="assistant",
                    content="다른 환자의 답변",
                ),
                ChatMessage(
                    public_id="user_msg_0000000000000001",
                    patient_id=get_settings().patient_id,
                    role="user",
                    sender_type="user",
                    content="사용자 메시지",
                ),
                ChatMessage(
                    public_id="assistant_msg_0000000000000002",
                    patient_id=get_settings().patient_id,
                    role="assistant",
                    sender_type="assistant",
                    content="다른 대화의 답변",
                ),
            ]
        )
        session.commit()

    other_patient = client.post(UI_CHAT_FEEDBACK_PATH, json=_payload())
    user_payload = _payload()
    user_payload["assistant_message_id"] = "user_msg_0000000000000001"
    user_message = client.post(UI_CHAT_FEEDBACK_PATH, json=user_payload)
    other_conversation_payload = _payload()
    other_conversation_payload[
        "assistant_message_id"
    ] = "assistant_msg_0000000000000002"
    other_conversation = client.post(
        UI_CHAT_FEEDBACK_PATH,
        json=other_conversation_payload,
    )

    assert other_patient.status_code == 404
    assert user_message.status_code == 422
    assert other_conversation.status_code == 202
    assert len(agent_client.calls) == 1
    assert agent_client.calls[0]["message_id"] == "assistant_msg_0000000000000002"


def test_ui_feedback_preserves_agent_idempotency_conflict(
    tmp_path: Path,
) -> None:
    client, agent_client, sessions = _app_with_database(tmp_path)
    with sessions() as session:
        session.add(
            ChatMessage(
                public_id="assistant_msg_0000000000000001",
                patient_id=get_settings().patient_id,
                role="assistant",
                sender_type="assistant",
                content="AI 답변",
            )
        )
        session.commit()
    agent_client.error = AgentServiceError(
        "conflict",
        status_code=409,
        error_type="IDEMPOTENCY_CONFLICT",
        retryable=False,
    )

    response = client.post(UI_CHAT_FEEDBACK_PATH, json=_payload())

    assert response.status_code == 409
    assert response.json() == {
        "success": False,
        "data": None,
        "error": {
            "code": "IDEMPOTENCY_CONFLICT",
            "message": "The request_id was already used with another feedback body.",
            "retryable": False,
            "details": None,
            },
        }
    with sessions() as session:
        message = session.query(ChatMessage).filter_by(
            public_id="assistant_msg_0000000000000001"
        ).one()
        feedback_status = feedback_status_from_metadata(message.metadata_json)
        assert feedback_status["opinion_submitted"] is False
        assert feedback_status["opinion_submitted_at"] is None


def test_ui_feedback_contract_exposes_only_browser_safe_fields(
    tmp_path: Path,
) -> None:
    client, _agent_client, _sessions = _app_with_database(tmp_path)
    document = client.app.openapi()
    operation = document["paths"][UI_CHAT_FEEDBACK_PATH]["post"]
    request_ref = operation["requestBody"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    schema_name = request_ref.rsplit("/", 1)[-1]
    components = document["components"]["schemas"]
    schema = components[schema_name]

    assert set(schema["properties"]) == {
        "request_id",
        "assistant_message_id",
        "reaction",
        "opinion_text",
        "feedback_at",
    }
    assert schema["additionalProperties"] is False
    opinion_schema = schema["properties"]["opinion_text"]
    assert any(
        option.get("maxLength") == 4_000
        for option in opinion_schema["anyOf"]
    )
    reaction_schema = schema["properties"]["reaction"]
    assert any(
        option.get("enum") == ["like", "dislike"]
        for option in reaction_schema["anyOf"]
    )
    assert {
        "request_id",
        "assistant_message_id",
        "feedback_at",
    } == set(schema["required"])
    response_422 = operation["responses"]["422"]["content"][
        "application/json"
    ]["schema"]
    assert response_422["$ref"].endswith("/UiFeedbackErrorResponse")
    assert operation["responses"]["500"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/UiFeedbackErrorResponse")
    success_response = components["UiChatOpinionAcceptedResponse"]
    accepted = components["UiChatOpinionAccepted"]
    error_response = components["UiFeedbackErrorResponse"]
    error = components["UiFeedbackError"]
    assert set(success_response["required"]) == {"success", "data", "error"}
    assert set(accepted["required"]) == {
        "status",
        "request_id",
        "assistant_message_id",
        "reaction",
        "opinion_submitted",
        "opinion_submitted_at",
    }
    assert set(error_response["required"]) == {"success", "data", "error"}
    assert set(error["required"]) == {
        "code",
        "message",
        "retryable",
        "details",
    }


def test_real_app_ui_feedback_validation_uses_ui_error_envelope() -> None:
    response = TestClient(system_main.create_app()).post(
        UI_CHAT_FEEDBACK_PATH,
        json={},
    )

    assert response.status_code == 422
    assert response.json()["success"] is False
    assert response.json()["data"] is None
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert response.json()["error"]["details"]["violations"]

    missing_feedback = TestClient(system_main.create_app()).post(
        UI_CHAT_FEEDBACK_PATH,
        json={
            "request_id": "req_0000000000000006",
            "assistant_message_id": "assistant_msg_0000000000000001",
            "feedback_at": "2026-07-26T09:30:00+00:00",
        },
    )
    assert missing_feedback.status_code == 422
    assert missing_feedback.json()["error"]["code"] == "INVALID_REQUEST"


def test_backend_agent_feedback_client_uses_internal_bearer(
    monkeypatch,
) -> None:
    captured: dict = {}

    class DummyAsyncClient:
        def __init__(self, **kwargs) -> None:
            captured["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, **kwargs):
            captured["url"] = url
            captured["request_kwargs"] = kwargs
            return httpx.Response(
                202,
                json={"status": "accepted"},
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        DummyAsyncClient,
    )
    payload = {
        "request_id": "req_0000000000000001",
        "message_id": "assistant_msg_0000000000000001",
        "patient_id": "patient_0000000000000001",
        "feedback_text": "의견",
        "feedback_at": "2026-07-26T09:30:00+00:00",
    }

    result = asyncio.run(
        AgentClient(base_url="http://agent.test").send_chat_feedback(payload)
    )

    assert result == {
        "status": "accepted",
        "reaction": None,
        "accepted_at": None,
    }
    assert captured["url"] == (
        "http://agent.test/agent/async/chat_feedback"
    )
    assert captured["request_kwargs"]["json"] == payload
    assert captured["request_kwargs"]["headers"] == {
        "Authorization": "Bearer pytest-agent-sync-token"
    }
    assert captured["client_kwargs"]["trust_env"] is False


@pytest.mark.parametrize("status_code", [200, 201, 204])
def test_backend_agent_feedback_client_requires_http_202(
    monkeypatch,
    status_code: int,
) -> None:
    class DummyAsyncClient:
        def __init__(self, **_kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url: str, **_kwargs):
            return httpx.Response(
                status_code,
                json={"status": "accepted"} if status_code != 204 else None,
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(
        agent_client_module.httpx,
        "AsyncClient",
        DummyAsyncClient,
    )

    with pytest.raises(AgentServiceError) as exc_info:
        asyncio.run(
            AgentClient(
                base_url="http://agent.test"
            ).send_chat_feedback(
                {
                    "request_id": "req_0000000000000002",
                    "message_id": "assistant_msg_0000000000000002",
                    "patient_id": "patient_0000000000000002",
                    "feedback_text": "의견",
                    "feedback_at": "2026-07-26T09:30:00+00:00",
                }
            )
        )

    assert exc_info.value.error_type == "agent_response_status_invalid"
    assert exc_info.value.status_code == status_code
