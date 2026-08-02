from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent_app.integration.dose_selection_state import (
    CONSUMED,
    DoseSelectionStateStore,
)
from agent_app.persistence.models import AgentPendingSelection
from shared.schemas import AgentResponse
from shared.settings import get_settings

PATIENT_ID = "patient_0000000000000811"
ORIGIN_MESSAGE_ID = "user_msg_0000000000000811"
SOURCE_REQUEST_ID = "req_0000000000000811"
RESPONSE_MESSAGE_ID = "user_msg_0000000000000812"
MESSAGE_AT = datetime(2026, 4, 20, 9, 30, tzinfo=UTC)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    AgentPendingSelection.__table__.create(engine)
    factory = sessionmaker(
        bind=engine,
        expire_on_commit=False,
    )
    try:
        yield factory
    finally:
        engine.dispose()


def _response() -> AgentResponse:
    candidates = [
        {
            "dose_event_id": "dose_morning",
            "medication_name": "메트포르민 500mg",
            "slot_label": "아침",
            "scheduled_for": "2026-04-20T08:00:00+09:00",
            "status": "scheduled",
            "selection_value": "메트포르민 500mg · 아침 08:00",
        },
        {
            "dose_event_id": "dose_evening",
            "medication_name": "메트포르민 500mg",
            "slot_label": "저녁",
            "scheduled_for": "2026-04-20T18:00:00+09:00",
            "status": "scheduled",
            "selection_value": "메트포르민 500mg · 저녁 18:00",
        },
    ]
    selections = [item["selection_value"] for item in candidates]
    return AgentResponse(
        trace_id="trace-dose-selection",
        agent_name="medication_agent",
        prompt_version_id="test",
        decision_type="selection_required",
        structured_payload={
            "selection_required": True,
            "selection_request": {
                "message_title": "복약 항목 선택",
                "text": "복용한 약을 선택해 주세요.",
                "tables": None,
                "selections": selections,
                "inputs": None,
            },
            "dose_selection": {
                "action_name": (
                    "update_medication_dose_event_status"
                ),
                "candidates": candidates,
            },
            "chat_response": {
                "message_type": "selection_box",
                "message": {
                    "message_title": "복약 항목 선택",
                    "text": "복용한 약을 선택해 주세요.",
                    "tables": None,
                    "selections": selections,
                    "inputs": None,
                },
            },
        },
        human_summary="복용한 약을 선택해 주세요.",
    )


def test_dose_candidate_snapshot_is_encrypted_resolved_and_consumed(
    session_factory,
) -> None:
    store = DoseSelectionStateStore(
        session_factory,
        settings=get_settings(),
    )
    prepared = store.prepare_from_agent_response(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        source_chat_request_id=SOURCE_REQUEST_ID,
        trace_id="trace-dose-selection",
        message_at=MESSAGE_AT,
        response=_response(),
    )
    assert prepared is not None
    assert prepared.candidate_count == 2

    with session_factory() as session:
        row = session.scalar(select(AgentPendingSelection))
        assert row is not None
        assert row.selection_type == "medication_dose"
        assert row.status == "PENDING"
        assert "메트포르민" not in row.payload_ciphertext
        assert "dose_morning" not in row.payload_ciphertext
        assert row.encryption_key_id.endswith(
            ":medication-dose-selection-v1"
        )

    resolved = store.resolve(
        patient_id=PATIENT_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="메트포르민 500mg · 아침 08:00",
    )
    assert resolved is not None
    assert resolved.dose_event_id == "dose_morning"

    store.consume(
        patient_id=PATIENT_ID,
        origin_message_id=ORIGIN_MESSAGE_ID,
        current_user_message_id=RESPONSE_MESSAGE_ID,
    )
    with session_factory() as session:
        row = session.scalar(select(AgentPendingSelection))
        assert row is not None
        assert row.status == CONSUMED
