from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import delete, select

from agent_app.integration.approval_state import (
    CANCELLED,
    CONSUMED,
    SENDING,
    InternalApprovalBinding,
    InternalApprovalError,
    InternalApprovalStore,
)
from agent_app.integration.write_state import canonical_payload_hash
from agent_app.persistence.db import SessionLocal
from agent_app.persistence.models import AgentPendingAction
from shared.time_utils import utc_now

PATIENT_ID = "patient_0000000000000001"
ORIGIN_REQUEST_ID = "req_0000000000000001"
ORIGIN_MESSAGE_ID = "user_msg_0000000000000001"
APPROVAL_REQUEST_ID = "req_0000000000000002"
APPROVAL_MESSAGE_ID = "user_msg_0000000000000002"
ACTION_NAME = "create_nutrition_meal_record"
ARGUMENTS = {
    "meal_type": "lunch",
    "meal_date": "2026-07-30",
    "foods": [{"food_name": "삶은 계란", "portion": "1개"}],
}
DISPLAY = {
    "title": "식사 기록",
    "question": "확인한 식사 내용을 기록할까요?",
    "action_label": "기록",
}


@pytest.fixture(autouse=True)
def clear_internal_approvals() -> None:
    with SessionLocal() as session:
        session.execute(delete(AgentPendingAction))
        session.commit()


def _source_message() -> dict:
    return {
        "message_type": "selection_box",
        "message": {
            "message_title": DISPLAY["title"],
            "text": DISPLAY["question"],
            "selections": ["기록", "취소"],
        },
    }


def _approve(store: InternalApprovalStore) -> tuple[str, str]:
    proposal = store.prepare(
        patient_id=PATIENT_ID,
        source_chat_request_id=ORIGIN_REQUEST_ID,
        source_message_id=ORIGIN_MESSAGE_ID,
        trace_id="trace-approval",
        action_name=ACTION_NAME,
        tool_call_id="tool-call-1",
        arguments=ARGUMENTS,
        display=DISPLAY,
    )
    decision = store.submit_response(
        patient_id=PATIENT_ID,
        current_user_message_id=APPROVAL_MESSAGE_ID,
        current_source_chat_request_id=APPROVAL_REQUEST_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="기록",
        source_message=_source_message(),
    )
    assert decision is not None
    assert decision.kind == "approved"
    assert decision.approval_key.startswith("apv_")
    return decision.approval_key, proposal.action_fingerprint


def _binding(
    approval_key: str,
    action_fingerprint: str,
    **changes,
) -> InternalApprovalBinding:
    values = {
        "approval_key": approval_key,
        "action_name": ACTION_NAME,
        "action_fingerprint": action_fingerprint,
        "patient_id": PATIENT_ID,
        "source_chat_request_id": APPROVAL_REQUEST_ID,
        "confirmation_message_id": APPROVAL_MESSAGE_ID,
        "write_request_id": "req_0000000000000003",
        "argument_hash": canonical_payload_hash(ARGUMENTS),
        "request_body_hash": canonical_payload_hash({"request": "body"}),
        "expected_version": None,
    }
    values.update(changes)
    return InternalApprovalBinding(**values)


def test_agent_owns_encrypted_pending_action_and_issues_no_key_before_approval() -> None:
    store = InternalApprovalStore(SessionLocal)
    proposal = store.prepare(
        patient_id=PATIENT_ID,
        source_chat_request_id=ORIGIN_REQUEST_ID,
        source_message_id=ORIGIN_MESSAGE_ID,
        trace_id="trace-approval",
        action_name=ACTION_NAME,
        tool_call_id="tool-call-1",
        arguments=ARGUMENTS,
        display=DISPLAY,
    )

    public = proposal.public_payload()
    assert public["confirmation_required"] is True
    assert "confirmation_id" not in public
    assert "approval_key" not in public
    assert public["display"] == DISPLAY
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingAction))
        assert row is not None
        assert row.payload_ciphertext.startswith("v1.")
        assert "삶은 계란" not in row.payload_ciphertext
        assert row.display_json == "{}"
        assert row.approval_key_hash == ""


def test_approved_short_key_resolves_arguments_and_is_consumed_once() -> None:
    store = InternalApprovalStore(SessionLocal)
    approval_key, fingerprint = _approve(store)
    grant = store.resolve_grant(
        approval_key=approval_key,
        patient_id=PATIENT_ID,
        action_name=ACTION_NAME,
        source_chat_request_id=APPROVAL_REQUEST_ID,
        confirmation_message_id=APPROVAL_MESSAGE_ID,
    )
    assert grant.arguments == ARGUMENTS

    binding = _binding(approval_key, fingerprint)
    store.claim(binding)
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingAction))
        assert row is not None
        assert row.status == SENDING
        assert row.approval_key_hash
        assert approval_key not in row.approval_key_hash
        assert row.argument_hash == binding.argument_hash

    store.consume(approval_key, result={"status": "ok"})
    store.validate_consumed_replay(binding)
    with pytest.raises(
        InternalApprovalError,
        match="internal_approval_already_consumed",
    ):
        store.claim(binding)
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingAction))
        assert row is not None
        assert row.status == CONSUMED


def test_key_is_bound_to_patient_action_message_and_exact_write() -> None:
    store = InternalApprovalStore(SessionLocal)
    approval_key, fingerprint = _approve(store)
    for changes, error in (
        ({"patient_id": "patient_0000000000000009"}, "patient_mismatch"),
        ({"action_name": "delete_nutrition_meal_record"}, "action_mismatch"),
        ({"source_chat_request_id": "req_0000000000000009"}, "message_binding"),
    ):
        values = {
            "approval_key": approval_key,
            "patient_id": PATIENT_ID,
            "action_name": ACTION_NAME,
            "source_chat_request_id": APPROVAL_REQUEST_ID,
            "confirmation_message_id": APPROVAL_MESSAGE_ID,
        }
        values.update(changes)
        with pytest.raises(InternalApprovalError, match=error):
            store.resolve_grant(**values)

    binding = _binding(approval_key, fingerprint)
    store.claim(binding)
    store.release_retry(approval_key)
    with pytest.raises(
        InternalApprovalError,
        match="internal_approval_request_mutated",
    ):
        store.claim(
            _binding(
                approval_key,
                fingerprint,
                request_body_hash=canonical_payload_hash({"changed": True}),
            )
        )


def test_same_approval_message_reissues_same_key_after_restart() -> None:
    first = InternalApprovalStore(SessionLocal)
    approval_key, _fingerprint = _approve(first)
    restarted = InternalApprovalStore(SessionLocal)
    decision = restarted.submit_response(
        patient_id=PATIENT_ID,
        current_user_message_id=APPROVAL_MESSAGE_ID,
        current_source_chat_request_id=APPROVAL_REQUEST_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="기록",
        source_message=_source_message(),
    )
    assert decision is not None
    assert decision.kind == "approved"
    assert decision.approval_key == approval_key


def test_cancel_never_issues_a_key_or_allows_execution() -> None:
    store = InternalApprovalStore(SessionLocal)
    store.prepare(
        patient_id=PATIENT_ID,
        source_chat_request_id=ORIGIN_REQUEST_ID,
        source_message_id=ORIGIN_MESSAGE_ID,
        trace_id="trace-cancel",
        action_name=ACTION_NAME,
        tool_call_id="tool-call-1",
        arguments=ARGUMENTS,
        display=DISPLAY,
    )
    decision = store.submit_response(
        patient_id=PATIENT_ID,
        current_user_message_id=APPROVAL_MESSAGE_ID,
        current_source_chat_request_id=APPROVAL_REQUEST_ID,
        originating_user_message_id=ORIGIN_MESSAGE_ID,
        submitted_value="취소",
        source_message=_source_message(),
    )
    assert decision is not None
    assert decision.kind == "cancelled"
    assert decision.approval_key == ""
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingAction))
        assert row is not None
        assert row.status == CANCELLED
        assert row.approval_key_hash == ""


def test_expired_approval_is_rejected_before_key_issue() -> None:
    store = InternalApprovalStore(SessionLocal)
    store.prepare(
        patient_id=PATIENT_ID,
        source_chat_request_id=ORIGIN_REQUEST_ID,
        source_message_id=ORIGIN_MESSAGE_ID,
        trace_id="trace-expired",
        action_name=ACTION_NAME,
        tool_call_id="tool-call-1",
        arguments=ARGUMENTS,
        display=DISPLAY,
    )
    with SessionLocal() as session:
        row = session.scalar(select(AgentPendingAction))
        assert row is not None
        row.action_expires_at = utc_now() - timedelta(seconds=1)
        session.commit()
    with pytest.raises(InternalApprovalError, match="internal_approval_expired"):
        store.submit_response(
            patient_id=PATIENT_ID,
            current_user_message_id=APPROVAL_MESSAGE_ID,
            current_source_chat_request_id=APPROVAL_REQUEST_ID,
            originating_user_message_id=ORIGIN_MESSAGE_ID,
            submitted_value="기록",
            source_message=_source_message(),
        )
