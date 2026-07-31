from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from agent_app.agents.multiturn_chat import MultiturnChatAgent
from shared.backend_v13_contracts import RecordChangeRequest
from shared.chat_contracts import ChatSyncRequest, chat_sync_response
from shared.json_utils import dump_json, parse_json_object
from shared.public_ids import new_public_id
from shared.schemas import AgentResponse
from shared.tool_names import CREATE_MEDICATION_SIDE_EFFECT_RECORD
from system_app.contracts_v13 import BackendChatRequest
from system_app.models import ChatMessage, SideEffectRecord
from system_app.services.backend_chat_service import (
    mark_user_message_failed,
    persist_assistant_response,
    persist_user_message,
)
from system_app.services.backend_v13_service import apply_record_change
from tests.helpers import build_system_engine


def _engine(name: str):
    return build_system_engine(name)


def _severity() -> dict:
    return {
        "questions": [
            {
                "item_code": "PROCTCAE_NAUSEA",
                "question": "지난 일주일 동안 메스꺼움이 얼마나 심했습니까?",
                "response_type": "single_choice",
                "response_options": [
                    "전혀 없다",
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
    }


def test_backend_persists_only_reply_edge_for_agent_owned_approval() -> None:
    engine, cleanup = _engine("side_effect_reply_edge")
    try:
        with Session(engine) as session:
            patient_id = new_public_id("patient")
            origin_request_id = new_public_id("request")
            origin = ChatMessage(
                public_id=new_public_id("user_message"),
                patient_id=patient_id,
                ai_request_id=origin_request_id,
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                message_type="text",
                content="부작용 평가 결과를 기록해줘",
                message_payload_json=dump_json(
                    {"text": "부작용 평가 결과를 기록해줘"}
                ),
                processing_status="pending",
                metadata_json="{}",
                created_at=datetime(2026, 7, 26, 1, 30),
            )
            session.add(origin)
            session.flush()

            response = chat_sync_response(
                ChatSyncRequest(
                    request_id=origin_request_id,
                    message_id=origin.public_id,
                    patient_id=patient_id,
                    requested_return_type="text",
                    message=origin.content,
                    message_at=datetime(2026, 7, 26, 1, 30, tzinfo=UTC),
                ),
                AgentResponse(
                    trace_id="trace-origin",
                    agent_name="medication_agent",
                    prompt_version_id="test",
                    decision_type="mutation_confirmation_required",
                    structured_payload={
                        "mutation_confirmation_required": True,
                        "mutation_confirmation": {
                            "action_name": (
                                CREATE_MEDICATION_SIDE_EFFECT_RECORD
                            ),
                            "display": {
                                "title": "부작용 평가 기록",
                                "question": "평가 결과를 기록할까요?",
                                "action_label": "기록",
                            },
                        },
                    },
                    human_summary="평가 결과를 기록할까요?",
                ),
            )
            persisted = persist_assistant_response(
                session,
                user_message=origin,
                response=response,
            )
            session.flush()
            source = session.scalar(
                select(ChatMessage).where(
                    ChatMessage.public_id
                    == persisted.assistant_message_id
                )
            )
            assert source is not None
            assert "mutation_confirmation" not in parse_json_object(
                source.metadata_json
            )

            request_id = new_public_id("request")
            reply, returned_request_id, _ = persist_user_message(
                session,
                BackendChatRequest(
                    patient_id=patient_id,
                    message="기록",
                    requested_return_type="selection_box",
                    source_message_id=source.public_id,
                    request_id=request_id,
                    message_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        32,
                        tzinfo=UTC,
                    ),
                ),
            )
            session.flush()
            assert returned_request_id == request_id
            assert reply.reply_to_message_id == source.id
            reply_metadata = parse_json_object(reply.metadata_json)
            assert "approved_mutation_confirmation" not in reply_metadata
            assert "approved_user_action" not in reply_metadata
            assert "mutation_confirmations" not in inspect(engine).get_table_names()

            mark_user_message_failed(
                session,
                user_message_id=reply.id,
                error_code="AI_PROCESSING_ERROR",
                retryable=True,
            )
            replay, replay_request_id, _ = persist_user_message(
                session,
                BackendChatRequest(
                    patient_id=patient_id,
                    message="기록",
                    requested_return_type="selection_box",
                    source_message_id=source.public_id,
                    request_id=request_id,
                    message_at=datetime(
                        2026,
                        7,
                        26,
                        1,
                        32,
                        tzinfo=UTC,
                    ),
                ),
            )
            assert replay.id == reply.id
            assert replay_request_id == returned_request_id
            assert replay.processing_status == "pending"
    finally:
        cleanup()


@pytest.mark.asyncio
async def test_confirmed_side_effect_action_uses_only_approval_key() -> None:
    class FinalChunk:
        content = "기록했습니다."
        tool_calls = []
        tool_call_chunks = []
        usage_metadata = {}

    class FinalModel:
        async def astream(self, _messages):
            yield FinalChunk()

    class FinalProvider:
        def chat_model(self):
            return FinalModel()

    class MedicationAgentStub:
        def __init__(self) -> None:
            self.calls = []

        async def execute_approved_write(
            self,
            trace_id,
            request_payload,
            *,
            tool_call,
        ):
            self.calls.append((trace_id, request_payload, tool_call))
            return AgentResponse(
                trace_id=trace_id,
                agent_name="medication_agent",
                prompt_version_id="test",
                decision_type="medication_chat",
                structured_payload={
                    "tool_calls": [
                        {
                            "name": tool_call["name"],
                            "arguments": {
                                "approval_key_present": True
                            },
                        }
                    ]
                },
                human_summary="기록했습니다.",
            )

    medication_agent = MedicationAgentStub()
    agent = object.__new__(MultiturnChatAgent)
    agent.provider = FinalProvider()
    agent.medication_agent = medication_agent
    agent.nutrition_management_agent = None

    approval_key = "apv_abcdefghijklmnopqrstuvwx"
    response = await agent.run(
        "trace-approved",
        {
            "patient_id": new_public_id("patient"),
            "event_type": "multiturn_chat",
            "message": "기록",
            "current_time": "2026-07-26T10:32:00+09:00",
            "context": {
                "approved_user_action": {
                    "action_name": (
                        CREATE_MEDICATION_SIDE_EFFECT_RECORD
                    ),
                    "status": "confirmed",
                    "arguments": {"approval_key": approval_key},
                },
            },
        },
    )

    forced_call = medication_agent.calls[0][2]
    assert forced_call["arguments"] == {"approval_key": approval_key}
    assert approval_key not in dump_json(response.structured_payload)


def test_v13_write_uses_confirmation_message_reply_edge_only() -> None:
    engine, cleanup = _engine("side_effect_write_reply_edge")
    try:
        with Session(engine) as session:
            patient_id = new_public_id("patient")
            source_request_id = new_public_id("request")
            card = ChatMessage(
                public_id=new_public_id("assistant_message"),
                patient_id=patient_id,
                ai_request_id=new_public_id("request"),
                role="assistant",
                sender_type="assistant",
                category="multiturn_chat",
                message_type="selection_box",
                content="평가 결과를 기록할까요?",
                message_payload_json=dump_json(
                    {
                        "message_title": "부작용 평가 기록",
                        "text": "평가 결과를 기록할까요?",
                        "selections": ["기록", "취소"],
                    }
                ),
                processing_status="completed",
            )
            session.add(card)
            session.flush()
            reply = ChatMessage(
                public_id=new_public_id("user_message"),
                patient_id=patient_id,
                ai_request_id=source_request_id,
                role="user",
                sender_type="patient",
                category="multiturn_chat",
                message_type="selection_box",
                content="기록",
                message_payload_json=dump_json({"text": "기록"}),
                reply_to_message_id=card.id,
                processing_status="completed",
            )
            session.add(reply)
            session.commit()

            request = RecordChangeRequest(
                request_id=new_public_id("request"),
                source_chat_request_id=source_request_id,
                confirmation_message_id=reply.public_id,
                patient_id=patient_id,
                resource_type="medication_side_effect",
                operation="create",
                record_id=None,
                parent_record_id=None,
                expected_version=None,
                payload={
                    "medication_name": "메트포르민 500mg",
                    "symptom_text": "메스꺼움",
                    "symptom_onset_text": "어제 복용 후",
                    "suspected": True,
                    "severity": _severity(),
                    "matched_effects": ["메스꺼움"],
                    "matched_items": ["메트포르민 500mg"],
                    "related_dose_event_id": None,
                },
                requested_at=datetime(
                    2026,
                    7,
                    26,
                    1,
                    32,
                    tzinfo=UTC,
                ),
            )
            assert "approval_key" not in request.model_dump(mode="json")
            result = apply_record_change(session, request)
            session.flush()
            assert result.result.record_id.startswith("sidefx_")
            assert session.query(SideEffectRecord).count() == 1
    finally:
        cleanup()
