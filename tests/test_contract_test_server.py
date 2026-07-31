from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from contract_test_server.callbacks import CallbackDispatcher
from contract_test_server.config import ContractServerSettings
from contract_test_server.main import create_app
from contract_test_server.storage import (
    CallbackInsert,
    ContractStore,
    canonical_request_hash,
)
from shared.async_v13_contracts import (
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
)
from shared.chat_contracts import ChatStreamEvent

AGENT_TOKEN = "agent-token-" + ("a" * 32)
CONTROL_TOKEN = "control-token-" + ("b" * 32)
BACKEND_TOKEN = "backend-token-" + ("c" * 32)
AUTH = {"Authorization": f"Bearer {AGENT_TOKEN}"}
CONTROL_AUTH = {"X-Test-Control-Token": CONTROL_TOKEN}


@pytest.fixture
def contract_database_url() -> str:
    raw_url = (
        os.getenv("CONTRACT_POSTGRES_TEST_DATABASE_URL", "").strip()
        or os.getenv("SYSTEM_POSTGRES_TEST_DATABASE_URL", "").strip()
    )
    if not raw_url:
        pytest.skip(
            "CONTRACT_POSTGRES_TEST_DATABASE_URL is required for "
            "contract-server PostgreSQL tests"
        )
    base_url = make_url(raw_url)
    if not base_url.drivername.startswith("postgresql"):
        raise RuntimeError(
            "CONTRACT_POSTGRES_TEST_DATABASE_URL must use PostgreSQL"
        )
    schema_name = f"contract_test_{uuid4().hex}"
    admin_engine = create_engine(
        base_url,
        future=True,
        poolclass=NullPool,
    )
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    query = dict(base_url.query)
    query["options"] = f"-csearch_path={schema_name}"
    isolated_url = base_url.set(query=query).render_as_string(
        hide_password=False
    )
    try:
        yield isolated_url
    finally:
        with admin_engine.begin() as connection:
            connection.execute(
                text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
            )
        admin_engine.dispose()


def _settings(
    database_url: str,
    **overrides: object,
) -> ContractServerSettings:
    values: dict[str, object] = {
        "app_release_version": "pytest",
        "contract_database_url": database_url,
        "contract_migration_database_url": database_url,
        "contract_specs_dir": Path("docs"),
        "agent_sync_api_token": AGENT_TOKEN,
        "test_control_token": CONTROL_TOKEN,
        "feedback_digest_secret": "feedback-digest-" + ("d" * 32),
        "callback_mode": "hold",
        "callback_retry_base_seconds": 0.1,
    }
    values.update(overrides)
    settings = ContractServerSettings(**values)
    store = ContractStore(settings.contract_migration_database_url)
    try:
        store.initialize()
    finally:
        store.dispose()
    return settings


def _chat_payload(
    *,
    request_id: str = "req_1111111111111111",
    return_type: str = "text",
    message: str = "안녕하세요",
) -> dict[str, object]:
    if return_type == "input_box":
        message = '{"value":1}'
    return {
        "request_id": request_id,
        "message_id": "user_msg_1111111111111111",
        "patient_id": "patient_1111111111111111",
        "requested_return_type": return_type,
        "message": message,
        "message_at": "2026-07-29T12:00:00+09:00",
    }


def _feedback_payload() -> dict[str, object]:
    return {
        "request_id": "req_2222222222222222",
        "message_id": "assistant_msg_2222222222222222",
        "patient_id": "patient_1111111111111111",
        "reaction": "like",
        "feedback_text": "  민감한 자유문 피드백  ",
        "feedback_at": "2026-07-29T12:01:00+09:00",
    }


def _missed_payload(
    request_id: str = "req_3333333333333333",
) -> dict[str, str]:
    return {
        "request_id": request_id,
        "patient_id": "patient_1111111111111111",
        "dose_event_id": "dose_3333333333333333",
    }


def _daily_payload(
    request_id: str = "req_4444444444444444",
) -> dict[str, object]:
    return {
        "request_id": request_id,
        "patient_id": [
            "patient_1111111111111111",
            "patient_2222222222222222",
        ],
        "analysis_date": "2026-07-29",
    }


def _ndjson_rows(response: httpx.Response) -> list[dict[str, object]]:
    assert response.content.endswith(b"\n")
    assert b"\n\n" not in response.content
    return [
        json.loads(line)
        for line in response.content.splitlines()
        if line
    ]


def test_hold_mode_is_ready_without_backend_address(
    contract_database_url: str,
) -> None:
    with TestClient(
        create_app(_settings(contract_database_url))
    ) as client:
        health = client.get("/health")
        ready = client.get("/health/ready")

    assert health.status_code == 200
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "service": "dranswer-agent-contract-test-server",
        "release": "pytest",
        "database_ready": True,
        "callback_mode": "hold",
        "callback_delivery_ready": False,
        "errors": [],
    }


@pytest.mark.parametrize("return_type", ["text", "selection_box", "input_box"])
def test_chat_ndjson_and_terminal_only_replay(
    contract_database_url: str,
    return_type: str,
) -> None:
    request_id = {
        "text": "req_1010101010101010",
        "selection_box": "req_2020202020202020",
        "input_box": "req_3030303030303030",
    }[return_type]
    payload = _chat_payload(
        request_id=request_id,
        return_type=return_type,
    )
    headers = {**AUTH, "Accept": "application/x-ndjson"}
    with TestClient(
        create_app(_settings(contract_database_url))
    ) as client:
        first = client.post("/agent/sync/chat", json=payload, headers=headers)
        replay = client.post("/agent/sync/chat", json=payload, headers=headers)

    assert first.status_code == 200
    assert first.headers["content-type"].startswith("application/x-ndjson")
    assert first.headers["cache-control"] == "no-cache"
    assert first.headers["x-accel-buffering"] == "no"
    rows = _ndjson_rows(first)
    assert [row["sequence"] for row in rows] == [0, 1]
    assert [row["status"] for row in rows] == ["streaming", "completed"]
    events = [ChatStreamEvent.model_validate(row) for row in rows]
    assert events[-1].message_type == return_type

    replay_rows = _ndjson_rows(replay)
    assert len(replay_rows) == 1
    assert replay_rows[0]["sequence"] == 0
    assert replay_rows[0]["status"] == "completed"
    assert replay_rows[0]["event_at"] == rows[-1]["event_at"]


def test_auth_validation_and_conflict_use_contract_error_envelope(
    contract_database_url: str,
) -> None:
    app = create_app(_settings(contract_database_url))
    payload = _chat_payload()
    with TestClient(app) as client:
        unauthorized = client.post(
            "/agent/sync/chat",
            json=payload,
            headers={"Accept": "application/x-ndjson"},
        )
        invalid = client.post(
            "/agent/sync/chat",
            json={**payload, "conversation_id": "retired"},
            headers={**AUTH, "Accept": "application/x-ndjson"},
        )
        accepted = client.post(
            "/agent/sync/chat",
            json=payload,
            headers={**AUTH, "Accept": "application/x-ndjson"},
        )
        conflict = client.post(
            "/agent/sync/chat",
            json={**payload, "message": "다른 본문"},
            headers={**AUTH, "Accept": "application/x-ndjson"},
        )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"
    assert invalid.status_code == 400
    assert set(invalid.json()) == {"request_id", "error"}
    assert invalid.json()["error"]["code"] == "INVALID_REQUEST"
    assert accepted.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_feedback_replay_persists_response_but_not_free_text(
    contract_database_url: str,
) -> None:
    settings = _settings(contract_database_url)
    payload = _feedback_payload()
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/agent/async/chat_feedback",
            json=payload,
            headers=AUTH,
        )
        replay = client.post(
            "/agent/async/chat_feedback",
            json={**payload, "feedback_text": "민감한 자유문 피드백"},
            headers=AUTH,
        )
        conflict = client.post(
            "/agent/async/chat_feedback",
            json={**payload, "reaction": "dislike"},
            headers=AUTH,
        )

    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert first.json()["accepted_at"].endswith(("+00:00", "Z"))
    assert conflict.status_code == 409
    plain_digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    store = ContractStore(settings.contract_database_url)
    try:
        with store.engine.connect() as connection:
            stored = connection.execute(
                text(
                    """
                    SELECT request_hash, response_json
                    FROM request_records
                    WHERE api_path = '/agent/async/chat_feedback'
                    """
                )
            ).mappings().one()
    finally:
        store.dispose()
    stored_digest = stored["request_hash"]
    assert str(payload["feedback_text"]) not in stored["response_json"]
    assert stored_digest != plain_digest


def test_async_acceptance_duplicate_conflict_and_held_callbacks(
    contract_database_url: str,
) -> None:
    app = create_app(_settings(contract_database_url))
    with TestClient(app) as client:
        first = client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )
        duplicate = client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )
        conflict = client.post(
            "/agent/async/missed-dose-events",
            json={
                **_missed_payload(),
                "dose_event_id": "dose_4444444444444444",
            },
            headers=AUTH,
        )
        callbacks = client.get(
            "/_test/callbacks",
            headers=CONTROL_AUTH,
        ).json()["callbacks"]

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "duplicate"
    assert conflict.status_code == 409
    assert len(callbacks) == 1
    assert callbacks[0]["status"] == "held"
    assert callbacks[0]["attempts"] == 0
    MissedDoseResultCallback.model_validate(callbacks[0]["payload"])


def test_daily_analysis_requires_unique_nonempty_array_and_queues_per_patient(
    contract_database_url: str,
) -> None:
    app = create_app(_settings(contract_database_url))
    with TestClient(app) as client:
        empty = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json={**_daily_payload(), "patient_id": []},
            headers=AUTH,
        )
        duplicate_patient = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json={
                **_daily_payload(),
                "patient_id": [
                    "patient_1111111111111111",
                    "patient_1111111111111111",
                ],
            },
            headers=AUTH,
        )
        accepted = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=_daily_payload(),
            headers=AUTH,
        )
        callbacks = client.get(
            "/_test/callbacks",
            headers=CONTROL_AUTH,
        ).json()["callbacks"]

    assert empty.status_code == 400
    assert duplicate_patient.status_code == 400
    assert accepted.status_code == 202
    assert len(callbacks) == 2
    assert all(item["status"] == "held" for item in callbacks)
    for item in callbacks:
        NotificationPolicyChangeProposalRequest.model_validate(item["payload"])


def test_postgresql_reconnect_keeps_idempotency_and_held_state(
    contract_database_url: str,
) -> None:
    settings = _settings(contract_database_url)
    with TestClient(create_app(settings)) as client:
        first = client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )
    with TestClient(create_app(settings)) as restarted:
        replay = restarted.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )
        callbacks = restarted.get(
            "/_test/callbacks",
            headers=CONTROL_AUTH,
        ).json()["callbacks"]

    assert first.status_code == 202
    assert replay.status_code == 200
    assert replay.json()["status"] == "duplicate"
    assert len(callbacks) == 1
    assert callbacks[0]["status"] == "held"
    assert callbacks[0]["attempts"] == 0


def test_postgresql_concurrent_idempotency_and_callback_claim_are_atomic(
    contract_database_url: str,
) -> None:
    _settings(contract_database_url)
    request_payload = _missed_payload(
        request_id="req_5656565656565656"
    )
    response_json = {
        "request_id": request_payload["request_id"],
        "status": "accepted",
    }
    callback = CallbackInsert(
        callback_request_id="req_5757575757575757",
        source_request_id=request_payload["request_id"],
        callback_kind="missed_dose_result",
        callback_path="/api/agent/async/missed-dose-results",
        payload={
            "request_id": "req_5757575757575757",
            "patient_id": request_payload["patient_id"],
            "dose_event_id": request_payload["dose_event_id"],
            "status": "completed",
            "result": {"message": "synthetic test result"},
            "error": None,
        },
        initial_status="held",
    )

    def register_once() -> str:
        store = ContractStore(contract_database_url)
        try:
            return store.register_request(
                api_path="/agent/async/missed-dose-events",
                request_id=request_payload["request_id"],
                request_hash=canonical_request_hash(request_payload),
                response_status=202,
                response_json=response_json,
                callbacks=[callback],
            ).outcome
        finally:
            store.dispose()

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(lambda _index: register_once(), range(8)))

    assert outcomes.count("new") == 1
    assert outcomes.count("replay") == 7

    coordinator = ContractStore(contract_database_url)
    assert len(coordinator.list_callbacks()) == 1
    assert (
        coordinator.release_callback(callback.callback_request_id)
        == "released"
    )

    def claim_once():
        store = ContractStore(contract_database_url)
        try:
            return store.claim_due_callback(lease_seconds=60)
        finally:
            store.dispose()

    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(lambda _index: claim_once(), range(2)))
    claimed = [value for value in claims if value is not None]
    assert len(claimed) == 1
    assert (
        claimed[0].callback_request_id
        == callback.callback_request_id
    )
    coordinator.dispose()


def test_held_callback_is_not_auto_released_after_delivery_configuration(
    contract_database_url: str,
) -> None:
    hold_settings = _settings(
        contract_database_url,
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
    )
    with TestClient(create_app(hold_settings)) as client:
        client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "request_id": "req_3333333333333333",
                "status": "processed",
            },
        )

    deliver_settings = _settings(
        contract_database_url,
        callback_mode="deliver",
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
    )
    store = ContractStore(deliver_settings.contract_database_url)
    dispatcher = CallbackDispatcher(
        settings=deliver_settings,
        store=store,
        transport=httpx.MockTransport(handler),
    )
    assert dispatcher.deliver_one().delivered is False
    assert calls == []

    with TestClient(create_app(deliver_settings)) as client:
        released = client.post(
            "/_test/callbacks/req_3333333333333333/release",
            headers=CONTROL_AUTH,
        )
    assert released.status_code == 200
    assert dispatcher.deliver_one().delivered is True
    assert len(calls) == 1
    assert calls[0].url.path == "/api/agent/async/missed-dose-results"
    assert calls[0].headers["authorization"] == f"Bearer {BACKEND_TOKEN}"
    assert store.list_callbacks()[0]["status"] == "completed"


def test_policy_callback_requires_correlated_http_status_and_ack(
    contract_database_url: str,
) -> None:
    settings = _settings(
        contract_database_url,
        callback_mode="deliver",
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
        callback_max_attempts=1,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        accepted = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json={
                **_daily_payload(),
                "patient_id": ["patient_1111111111111111"],
            },
            headers=AUTH,
        )
    assert accepted.status_code == 202
    store = ContractStore(settings.contract_database_url)
    callback = store.list_callbacks()[0]

    def invalid_ack(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": callback["callback_request_id"],
                "status": "accepted",
            },
        )

    dispatcher = CallbackDispatcher(
        settings=settings,
        store=store,
        transport=httpx.MockTransport(invalid_ack),
    )
    assert dispatcher.deliver_one().delivered is True
    finished = store.list_callbacks()[0]
    assert finished["status"] == "dead"
    assert finished["last_error_code"] == "retry_exhausted:callback_invalid_ack"


def test_permanent_callback_http_error_is_not_retried(
    contract_database_url: str,
) -> None:
    settings = _settings(
        contract_database_url,
        callback_mode="deliver",
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )
    store = ContractStore(settings.contract_database_url)
    dispatcher = CallbackDispatcher(
        settings=settings,
        store=store,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(401, json={"error": "no"})
        ),
    )
    dispatcher.deliver_one()
    callback = store.list_callbacks()[0]
    assert callback["status"] == "dead"
    assert callback["attempts"] == 1
    assert callback["last_error_code"] == "callback_permanent_http_401"


def test_invalid_ack_retries_byte_identical_payload_and_accepts_duplicate_ack(
    contract_database_url: str,
) -> None:
    settings = _settings(
        contract_database_url,
        callback_mode="deliver",
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
        callback_max_attempts=2,
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )

    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "request_id": "req_9999999999999999",
                    "status": "processed",
                },
            )
        return httpx.Response(
            200,
            json={
                "request_id": "req_3333333333333333",
                "status": "duplicate",
            },
        )

    store = ContractStore(settings.contract_database_url)
    dispatcher = CallbackDispatcher(
        settings=settings,
        store=store,
        transport=httpx.MockTransport(handler),
    )
    dispatcher.deliver_one()
    assert store.list_callbacks()[0]["status"] == "retry_wait"
    time.sleep(0.11)
    dispatcher.deliver_one()

    callback = store.list_callbacks()[0]
    assert callback["status"] == "completed"
    assert callback["attempts"] == 2
    assert len(bodies) == 2
    assert bodies[0] == bodies[1]


def test_callback_claim_lease_prevents_live_worker_recovery(
    contract_database_url: str,
) -> None:
    settings = _settings(
        contract_database_url,
        callback_mode="deliver",
        backend_callback_base_url="https://backend.example",
        backend_api_token=BACKEND_TOKEN,
    )
    with TestClient(create_app(settings)) as client:
        client.post(
            "/agent/async/missed-dose-events",
            json=_missed_payload(),
            headers=AUTH,
        )

    store = ContractStore(settings.contract_database_url)
    first_claim = store.claim_due_callback(lease_seconds=60)
    assert first_claim is not None
    assert store.recover_expired_in_progress() == 0
    assert store.claim_due_callback(lease_seconds=60) is None
    assert (
        store.mark_callback_completed(
            first_claim.callback_request_id,
            claim_id="not-the-live-claim",
            http_status=200,
        )
        is False
    )
    assert store.list_callbacks()[0]["status"] == "in_progress"
    assert (
        store.mark_callback_completed(
            first_claim.callback_request_id,
            claim_id=first_claim.claim_id,
            http_status=200,
        )
        is True
    )
    assert store.list_callbacks()[0]["status"] == "completed"


def test_static_contract_artifacts_and_public_openapi_boundary(
    contract_database_url: str,
) -> None:
    app = create_app(_settings(contract_database_url))
    schema = app.openapi()
    assert set(schema["paths"]) == {
        "/agent/sync/chat",
        "/agent/async/chat_feedback",
        "/agent/async/missed-dose-events",
        "/agent/async/daily-medication-pattern-analysis",
    }
    assert all(
        "422" not in schema["paths"][path]["post"]["responses"]
        for path in schema["paths"]
    )
    chat_200_content = schema["paths"]["/agent/sync/chat"]["post"][
        "responses"
    ]["200"]["content"]
    assert set(chat_200_content) == {"application/x-ndjson"}
    assert chat_200_content["application/x-ndjson"]["schema"][
        "x-ndjson-item-schema"
    ] == {"$ref": "#/components/schemas/ChatStreamEvent"}

    artifacts = {
        "chat.json": "AI_V13_CHAT_OPENAPI.json",
        "async-medication.json": "AI_V13_ASYNC_MEDICATION_OPENAPI.json",
        "backend-callbacks.json": "BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json",
    }
    with TestClient(app) as client:
        for route_name, file_name in artifacts.items():
            response = client.get(f"/contracts/v1.3/{route_name}")
            assert response.status_code == 200
            assert response.content == (Path("docs") / file_name).read_bytes()
