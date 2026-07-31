from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from shared.contract_errors import contract_error_definition
from system_app.main import app
from system_app.services.backend_v13_service import (
    BackendRequestConflict,
    BackendRequestGate,
)
from tests.helpers import build_system_engine

BACKEND_HEADERS = {
    "Authorization": "Bearer pytest-backend-api-token",
}


def test_v11_error_catalog_uses_spec_messages() -> None:
    assert contract_error_definition(
        "NUTRITION_MEAL_NOT_FOUND"
    ).message == "The requested nutrition meal was not found."
    assert contract_error_definition(
        "IDEMPOTENCY_CONFLICT"
    ).message == (
        "The request_id was reused with a different request body."
    )
    assert contract_error_definition("REQUEST_IN_PROGRESS").retryable is True


def test_write_validation_separates_schema_and_business_errors() -> None:
    client = TestClient(app)
    base = {
        "request_id": "req_0000000000001001",
        "source_chat_request_id": "req_0000000000001002",
        "confirmation_message_id": "user_msg_0000000000001003",
        "patient_id": "patient_0000000000001004",
        "resource_type": "nutrition_meal",
        "operation": "create",
        "record_id": None,
        "parent_record_id": None,
        "expected_version": None,
        "payload": {
            "meal_type": "lunch",
            "foods": [{"food_name": "두부"}],
        },
        "requested_at": "2026-07-28T10:30:00+09:00",
    }

    missing_required_key = client.post(
        "/agent/sync/record-change",
        json={key: value for key, value in base.items() if key != "record_id"},
        headers=BACKEND_HEADERS,
    )
    invalid_operation_state = client.post(
        "/agent/sync/record-change",
        json={**base, "record_id": "meal_0000000000001005"},
        headers=BACKEND_HEADERS,
    )
    policy_boundary = client.post(
        "/agent/sync/notification-policy-change",
        json={
            "request_id": "req_0000000000001006",
            "source_chat_request_id": "req_0000000000001007",
            "confirmation_message_id": "user_msg_0000000000001008",
            "patient_id": "patient_0000000000001009",
            "policy_id": "npol_000000000000100a",
            "expected_version": 1,
            "payload": {
                "decision": "apply",
                "changes": {"extra_reminders": 999},
                "reason": "경계 검증",
            },
            "requested_at": "2026-07-28T10:30:00+09:00",
        },
        headers=BACKEND_HEADERS,
    )

    assert missing_required_key.status_code == 400
    assert missing_required_key.json()["error"]["code"] == "INVALID_REQUEST"
    assert invalid_operation_state.status_code == 422
    assert (
        invalid_operation_state.json()["error"]["code"]
        == "BUSINESS_VALIDATION_FAILED"
    )
    assert policy_boundary.status_code == 422
    assert (
        policy_boundary.json()["error"]["code"]
        == "POLICY_BOUNDARY_VIOLATION"
    )


def test_invalid_policy_proposal_is_business_validation() -> None:
    response = TestClient(app).post(
        "/api/agent/async/notification-policy-change-proposals",
        json={
            "request_id": "req_000000000000100b",
            "patient_id": "patient_000000000000100c",
            "proposed_policy": {},
            "reason": "변경 제안",
        },
        headers=BACKEND_HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_POLICY_PROPOSAL"


def test_backend_idempotency_unique_key_is_cross_session_arbiter() -> None:
    engine, cleanup = build_system_engine("v11_backend_idempotency")
    barrier = threading.Barrier(2)

    def submit(payload: dict[str, str]) -> str:
        with Session(engine) as session:
            barrier.wait(timeout=5)
            try:
                replay = BackendRequestGate().begin(
                    session,
                    api_path="/agent/sync/record-change",
                    request_id="req_000000000000100d",
                    payload=payload,
                )
            except BackendRequestConflict as exc:
                session.rollback()
                return exc.code
            assert replay is None
            time.sleep(0.15)
            session.commit()
            return "CREATED"

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    submit,
                    ({"value": "same"}, {"value": "same"}),
                )
            )
        assert sorted(outcomes) == ["CREATED", "REQUEST_IN_PROGRESS"]
    finally:
        engine.dispose()
        cleanup()
