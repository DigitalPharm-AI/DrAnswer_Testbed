from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from agent_app.integration.pro_ctcae_survey import (
    APPROVAL_PENDING,
    COMPLETED,
    ProCtcaeSurveyService,
    ProCtcaeSurveyTransition,
    pro_ctcae_question_response,
)
from agent_app.persistence.models import (
    AgentProCtcaeResponse,
    AgentProCtcaeSurvey,
)
from agent_app.persistence.retention import purge_expired_agent_state
from agent_app.routes.chat import (
    _agent_response_for_survey_transition,
)
from shared.backend_v13_contracts import (
    ProCtcaeQuestion,
    ProCtcaeResponse,
    ProCtcaeSeverityResult,
)
from shared.chat_contracts import ChatSyncRequest
from shared.schemas import AgentResponse
from shared.settings import get_settings
from shared.tool_names import (
    CREATE_MEDICATION_SIDE_EFFECT_RECORD,
    GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
    GET_PRO_CTCAE_QUESTIONNAIRE,
)
from tests.helpers import build_agent_engine


def _assessment_response() -> AgentResponse:
    questionnaire = {
        "input_symptom": "어제 약을 먹고 속이 메스꺼웠어",
        "matched": True,
        "match_type": "exact",
        "matched_symptom_term": "Nausea",
        "matched_korean_symptom_name": "메스꺼움",
        "similarity": 1.0,
        "threshold": 0.7,
        "scoring_method": "local_similarity",
        "embedding_provider": "",
        "sheet_name": "Nausea",
        "questions": [
            {
                "symptom_term": "Nausea",
                "korean_symptom_name": "메스꺼움",
                "item_code": "PROCTCAE_NAUSEA_FREQ",
                "question": (
                    "지난 일주일 동안, 메스꺼움을 얼마나 자주 "
                    "느꼈습니까?"
                ),
                "response_type": "single_choice",
                "response_options": [
                    "전혀 없다",
                    "드물게 있다",
                    "가끔 있다",
                    "자주 있다",
                    "거의 항상 있다",
                ],
                "pdf_page": 1,
                "sheet_name": "Nausea",
            },
            {
                "symptom_term": "Nausea",
                "korean_symptom_name": "메스꺼움",
                "item_code": "PROCTCAE_NAUSEA_SEV",
                "question": (
                    "지난 일주일 동안, 메스꺼움의 정도가 "
                    "가장 심했을 때 어느 정도였습니까?"
                ),
                "response_type": "single_choice",
                "response_options": [
                    "전혀 없었다",
                    "경미했다",
                    "중간 정도였다",
                    "심했다",
                    "매우 심했다",
                ],
                "pdf_page": 1,
                "sheet_name": "Nausea",
            },
        ],
        "candidates": [],
    }
    return AgentResponse(
        trace_id="trace-pro-ctcae-1",
        agent_name="multiturn_chat_agent",
        prompt_version_id="test",
        decision_type="side_effect_assessment",
        structured_payload={
            "tool_calls": [
                {
                    "id": "call-assessment",
                    "name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                    "arguments": {
                        "symptom_text": "어제 약을 먹고 속이 메스꺼웠어",
                        "symptom_onset_text": "어제 복용 후",
                    },
                },
                {
                    "id": "call-questionnaire",
                    "name": GET_PRO_CTCAE_QUESTIONNAIRE,
                    "arguments": {
                        "symptom_text": "어제 약을 먹고 속이 메스꺼웠어",
                    },
                },
            ],
            "side_effect_lookup": {
                "tool_name": GET_MEDICATION_SIDE_EFFECT_ASSESSMENT,
                "status": "success",
                "response": {
                    "suspected": True,
                    "matched_items": [
                        "메트포르민 500mg",
                        "수니티닙 50mg",
                        "레트로졸 2.5mg",
                    ],
                    "matched_effects": [
                        "메트포르민 500mg: 메스꺼움",
                        "수니티닙 50mg: 메스꺼움",
                        "레트로졸 2.5mg: 메스꺼움",
                    ],
                },
            },
            "ae_pro_ctcae": questionnaire,
        },
        human_summary="메스꺼움 관련 질문이 준비되었습니다.",
    )


def test_pro_ctcae_questions_advance_by_item_code_and_complete_once() -> None:
    engine, cleanup = build_agent_engine("pro_ctcae_survey")
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    service = ProCtcaeSurveyService(
        sessions,
        settings=get_settings(),
    )
    try:
        first = service.start_from_agent_response(
            patient_id="patient_0000000000000001",
            origin_message_id="user_msg_0000000000000001",
            trace_id="trace-pro-ctcae-1",
            user_message="어제 약을 먹고 속이 메스꺼웠어",
            response=_assessment_response(),
        )
        assert first is not None
        assert first.kind == "next_question"
        assert first.question_number == 1
        assert first.question_count == 2
        assert first.question is not None
        assert first.question.item_code == "PROCTCAE_NAUSEA_FREQ"
        first_card = pro_ctcae_question_response(
            first,
            trace_id="trace-pro-ctcae-1",
        )
        assert first_card.structured_payload["survey_progress"] == {
            "current": 1,
            "total": 2,
        }

        second = service.submit_response(
            patient_id="patient_0000000000000001",
            current_user_message_id="user_msg_0000000000000002",
            originating_user_message_id=(
                "user_msg_0000000000000001"
            ),
            submitted_value="자주 있다",
        )
        assert second is not None
        assert second.kind == "next_question"
        assert second.question_number == 2
        assert second.question is not None
        assert second.question.item_code == "PROCTCAE_NAUSEA_SEV"

        completed = service.submit_response(
            patient_id="patient_0000000000000001",
            current_user_message_id="user_msg_0000000000000003",
            originating_user_message_id=(
                "user_msg_0000000000000002"
            ),
            submitted_value="심했다",
        )
        assert completed is not None
        assert completed.kind == "completed"
        assert completed.completed_context is not None
        assert completed.completed_context["symptom_name"] == (
            "메스꺼움"
        )
        assert completed.completed_context["medication_name"] is None
        assert completed.completed_context["matched_items"] == [
            "메트포르민 500mg",
            "수니티닙 50mg",
            "레트로졸 2.5mg",
        ]
        assert completed.severity is not None
        assert [
            response.item_code
            for response in completed.severity.responses
        ] == [
            "PROCTCAE_NAUSEA_FREQ",
            "PROCTCAE_NAUSEA_SEV",
        ]
        assert [
            response.response_text
            for response in completed.severity.responses
        ] == ["자주 있다", "심했다"]

        service.mark_approval_pending(
            patient_id="patient_0000000000000001",
            survey_id=completed.survey_id,
        )
        with sessions() as session:
            survey = session.scalar(
                select(AgentProCtcaeSurvey).where(
                    AgentProCtcaeSurvey.public_id
                    == completed.survey_id
                )
            )
            assert survey is not None
            assert survey.status == APPROVAL_PENDING
            assert session.scalar(
                select(func.count(AgentProCtcaeResponse.id))
            ) == 2
    finally:
        cleanup()


def test_pro_ctcae_duplicate_answer_message_is_idempotent() -> None:
    engine, cleanup = build_agent_engine("pro_ctcae_replay")
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    service = ProCtcaeSurveyService(
        sessions,
        settings=get_settings(),
    )
    try:
        service.start_from_agent_response(
            patient_id="patient_0000000000000002",
            origin_message_id="user_msg_0000000000000010",
            trace_id="trace-pro-ctcae-replay",
            user_message="속이 메스꺼웠어",
            response=_assessment_response(),
        )
        next_question = service.submit_response(
            patient_id="patient_0000000000000002",
            current_user_message_id="user_msg_0000000000000011",
            originating_user_message_id=(
                "user_msg_0000000000000010"
            ),
            submitted_value="가끔 있다",
        )
        replay = service.submit_response(
            patient_id="patient_0000000000000002",
            current_user_message_id="user_msg_0000000000000011",
            originating_user_message_id=(
                "user_msg_0000000000000010"
            ),
            submitted_value="가끔 있다",
        )
        assert next_question is not None
        assert replay is not None
        assert replay.kind == next_question.kind
        assert replay.question_number == next_question.question_number
        with sessions() as session:
            assert session.scalar(
                select(func.count(AgentProCtcaeResponse.id))
            ) == 1
            survey = session.scalar(select(AgentProCtcaeSurvey))
            assert survey is not None
            assert survey.status != COMPLETED
    finally:
        cleanup()


@pytest.mark.asyncio
async def test_completed_survey_continues_to_approval_with_server_context() -> None:
    completed_context = {
        "survey_id": "survey_0000000000000001",
        "symptom_text": "속이 메스꺼웠어",
        "symptom_onset_text": "어제 복용 후",
        "medication_name": None,
        "suspected": True,
        "matched_items": [
            "메트포르민 500mg",
            "수니티닙 50mg",
        ],
        "matched_effects": [
            "메트포르민 500mg: 메스꺼움",
            "수니티닙 50mg: 메스꺼움",
        ],
        "severity": ProCtcaeSeverityResult(
            questions=[
                ProCtcaeQuestion(
                    item_code="PROCTCAE_NAUSEA_FREQ",
                    question="메스꺼움을 얼마나 자주 느꼈습니까?",
                    response_type="single_choice",
                    response_options=["전혀 없다", "자주 있다"],
                )
            ],
            responses=[
                ProCtcaeResponse(
                    item_code="PROCTCAE_NAUSEA_FREQ",
                    response_index=1,
                    response_text="자주 있다",
                )
            ],
        ).model_dump(mode="json"),
    }
    transition = ProCtcaeSurveyTransition(
        kind="completed",
        survey_id="survey_0000000000000001",
        symptom_name="메스꺼움",
        question=None,
        question_number=1,
        question_count=1,
        severity=ProCtcaeSeverityResult.model_validate(
            completed_context["severity"]
        ),
        completed_context=completed_context,
    )

    class StubSurveyService:
        def __init__(self) -> None:
            self.marked: list[tuple[str, str]] = []

        def mark_approval_pending(
            self,
            *,
            patient_id: str,
            survey_id: str,
        ) -> None:
            self.marked.append((patient_id, survey_id))

    class StubMedicationAgent:
        def __init__(self) -> None:
            self.payload: dict | None = None
            self.continuation_tool_calls: list[dict] | None = None

        async def continue_with_tool_calls(
            self,
            trace_id: str,
            payload: dict,
            *,
            tool_calls: list[dict],
        ) -> AgentResponse:
            self.payload = payload
            self.continuation_tool_calls = tool_calls
            return AgentResponse(
                trace_id=trace_id,
                agent_name="medication_agent",
                prompt_version_id="test",
                decision_type="mutation_confirmation_required",
                structured_payload={
                    "mutation_confirmation": {
                        "action_name": (
                            CREATE_MEDICATION_SIDE_EFFECT_RECORD
                        )
                    }
                },
                human_summary="증상 기록을 저장할까요?",
            )

    class StubMultiturnAgent:
        def __init__(self) -> None:
            self.medication_agent = StubMedicationAgent()

    class StubOrchestrator:
        def __init__(self) -> None:
            self.multiturn_chat_agent = StubMultiturnAgent()

    survey_service = StubSurveyService()
    orchestrator = StubOrchestrator()
    payload = ChatSyncRequest(
        request_id="req_0000000000000100",
        message_id="user_msg_0000000000000100",
        patient_id="patient_0000000000000100",
        requested_return_type="selection_box",
        message="자주 있다",
        message_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    response = await _agent_response_for_survey_transition(
        survey_service,  # type: ignore[arg-type]
        orchestrator=orchestrator,  # type: ignore[arg-type]
        payload=payload,
        agent_payload={
            "patient_id": payload.patient_id,
            "message": payload.message,
            "context": {
                "request_metadata": {
                    "request_id": payload.request_id,
                    "message_id": payload.message_id,
                },
                "trusted_patient_context": {
                    "active_medication_schedules": []
                },
            },
        },
        trace_id="trace-pro-ctcae-complete",
        transition=transition,
    )

    assert response.decision_type == "mutation_confirmation_required"
    medication_agent = (
        orchestrator.multiturn_chat_agent.medication_agent
    )
    assert medication_agent.payload is not None
    assert medication_agent.payload["context"][
        "completed_pro_ctcae_survey"
    ] == completed_context
    assert medication_agent.continuation_tool_calls == [
        {
            "id": (
                "survey_survey_0000000000000001_approval"
            ),
            "name": "request_record_approval",
            "arguments": {
                "action_name": (
                    "create_medication_side_effect_record"
                ),
                "record_arguments": {
                    "symptom_text": "속이 메스꺼웠어",
                    "symptom_onset_text": "어제 복용 후",
                },
            },
        }
    ]
    assert survey_service.marked == [
        (
            "patient_0000000000000100",
            "survey_0000000000000001",
        )
    ]


def test_pro_ctcae_retention_expires_then_deletes_state() -> None:
    engine, cleanup = build_agent_engine("pro_ctcae_retention")
    sessions = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
        future=True,
    )
    now = datetime(2026, 7, 30, 12, 0)
    try:
        with sessions() as session:
            awaiting = AgentProCtcaeSurvey(
                public_id="survey_expire_now",
                patient_id_hash="patient-hash-awaiting",
                origin_message_id="user_msg_expire_now",
                expected_origin_message_id="user_msg_expire_now",
                trace_id="trace-expire-now",
                status="AWAITING_RESPONSE",
                question_count=1,
                current_question_index=0,
                payload_ciphertext="encrypted",
                payload_hash="a" * 64,
                encryption_key_id="pytest-feedback-v1",
                version=1,
                created_at=now - timedelta(days=1),
                updated_at=now - timedelta(days=1),
                response_expires_at=now - timedelta(seconds=1),
                expires_at=now + timedelta(days=1),
            )
            terminal = AgentProCtcaeSurvey(
                public_id="survey_delete_now",
                patient_id_hash="patient-hash-terminal",
                origin_message_id="user_msg_delete_now",
                expected_origin_message_id="user_msg_delete_now",
                trace_id="trace-delete-now",
                status="EXPIRED",
                question_count=1,
                current_question_index=1,
                payload_ciphertext="encrypted",
                payload_hash="b" * 64,
                encryption_key_id="pytest-feedback-v1",
                version=2,
                created_at=now - timedelta(days=2),
                updated_at=now - timedelta(days=1),
                resolved_at=now - timedelta(days=1),
                response_expires_at=now - timedelta(days=1),
                expires_at=now - timedelta(seconds=1),
            )
            session.add_all([awaiting, terminal])
            session.flush()
            session.add(
                AgentProCtcaeResponse(
                    survey_id=terminal.id,
                    item_code="PROCTCAE_NAUSEA_FREQ",
                    response_index=1,
                    source_user_message_id="user_msg_response_old",
                    answered_at=now - timedelta(days=1),
                )
            )
            session.commit()

        result = purge_expired_agent_state(
            sessions,
            now=now,
        )
        assert result["expired_pro_ctcae_surveys"] == 1
        assert result["deleted_pro_ctcae_surveys"] == 1
        with sessions() as session:
            retained = session.scalar(
                select(AgentProCtcaeSurvey).where(
                    AgentProCtcaeSurvey.public_id
                    == "survey_expire_now"
                )
            )
            assert retained is not None
            assert retained.status == "EXPIRED"
            assert session.scalar(
                select(func.count(AgentProCtcaeResponse.id))
            ) == 0
    finally:
        cleanup()
