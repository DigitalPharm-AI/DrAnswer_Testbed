from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from agent_app import main as agent_main
from agent_app.jobs.daily_pattern_tasks import (
    DAILY_PATTERN_ANALYSIS_TASK,
)
from agent_app.routes import tasks as task_routes
from shared.async_v13_contracts import (
    AsyncEventAccepted,
    DailyMedicationPatternAnalysisRequest,
)
from shared.public_ids import new_public_id

AGENT_HEADERS = {
    "Authorization": "Bearer pytest-agent-sync-token",
}


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self) -> _FakeSession:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _request_body(*, request_id: str | None = None) -> dict:
    return {
        "request_id": request_id or new_public_id("request"),
        "patient_id": [
            new_public_id("patient"),
            new_public_id("patient"),
        ],
        "analysis_date": "2026-07-28",
    }


def test_daily_pattern_request_requires_nonempty_unique_patient_list() -> None:
    body = _request_body()
    request = DailyMedicationPatternAnalysisRequest.model_validate(body)

    assert request.model_dump(mode="json") == body
    with pytest.raises(ValueError):
        DailyMedicationPatternAnalysisRequest.model_validate(
            {**body, "patient_id": []}
        )
    with pytest.raises(
        ValueError,
        match="daily_pattern_patient_id_must_be_unique",
    ):
        DailyMedicationPatternAnalysisRequest.model_validate(
            {
                **body,
                "patient_id": [
                    body["patient_id"][0],
                    body["patient_id"][0],
                ],
            }
        )
    with pytest.raises(ValueError):
        DailyMedicationPatternAnalysisRequest.model_validate(
            {**body, "analysis_date": "2026-02-30"}
        )


def test_daily_pattern_acceptance_fans_out_to_existing_patient_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    stored: dict[str, SimpleNamespace] = {}

    def fake_enqueue_async_task(
        _session,
        *,
        request_id,
        task_type,
        payload,
        callback_context,
        **_kwargs,
    ):
        existing = stored.get(request_id)
        if existing is not None:
            return existing, False
        task = SimpleNamespace(
            request_id=request_id,
            task_type=task_type,
            payload_json=json.dumps(payload, ensure_ascii=False),
            callback_context_json=json.dumps(callback_context),
        )
        stored[request_id] = task
        return task, True

    monkeypatch.setattr(
        task_routes,
        "enqueue_async_task",
        fake_enqueue_async_task,
    )
    request = DailyMedicationPatternAnalysisRequest.model_validate(
        _request_body()
    )

    accepted = task_routes._enqueue_daily_pattern_analysis(
        session,
        request,
    )
    duplicate = task_routes._enqueue_daily_pattern_analysis(
        session,
        request,
    )

    assert accepted == AsyncEventAccepted(
        request_id=request.request_id,
        status="accepted",
    )
    assert duplicate == AsyncEventAccepted(
        request_id=request.request_id,
        status="duplicate",
    )
    assert session.commits == 1
    assert len(stored) == len(request.patient_id)
    assert request.request_id in stored
    payloads = [
        json.loads(task.payload_json)
        for task in stored.values()
    ]
    assert {payload["patient_id"] for payload in payloads} == set(
        request.patient_id
    )
    assert all(
        task.task_type == DAILY_PATTERN_ANALYSIS_TASK
        for task in stored.values()
    )
    assert all(
        payload["analysis_date"]
        == request.analysis_date.isoformat()
        for payload in payloads
    )
    assert all(
        payload["_batch_patient_id"] == request.patient_id
        for payload in payloads
    )


def test_daily_pattern_request_id_reuse_with_changed_body_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    stored: dict[str, SimpleNamespace] = {}

    def fake_enqueue_async_task(
        _session,
        *,
        request_id,
        task_type,
        payload,
        callback_context,
        **_kwargs,
    ):
        existing = stored.get(request_id)
        if existing is not None:
            return existing, False
        task = SimpleNamespace(
            task_type=task_type,
            payload_json=json.dumps(payload),
            callback_context_json=json.dumps(callback_context),
        )
        stored[request_id] = task
        return task, True

    monkeypatch.setattr(
        task_routes,
        "enqueue_async_task",
        fake_enqueue_async_task,
    )
    body = _request_body()
    original = DailyMedicationPatternAnalysisRequest.model_validate(body)
    task_routes._enqueue_daily_pattern_analysis(session, original)
    changed = DailyMedicationPatternAnalysisRequest.model_validate(
        {
            **body,
            "analysis_date": "2026-07-27",
        }
    )

    with pytest.raises(HTTPException) as captured:
        task_routes._enqueue_daily_pattern_analysis(session, changed)

    assert captured.value.status_code == 409
    assert captured.value.detail["code"] == "IDEMPOTENCY_CONFLICT"


def test_daily_pattern_route_returns_queue_unavailable_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(task_routes, "_session", lambda: session)

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        task_routes,
        "_enqueue_daily_pattern_analysis",
        fail_enqueue,
    )

    response = TestClient(agent_main.app).post(
        task_routes.DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
        headers=AGENT_HEADERS,
        json=_request_body(),
    )

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "QUEUE_UNAVAILABLE",
        "message": "The asynchronous processing queue is unavailable.",
        "retryable": True,
        "details": None,
    }
    assert session.rollbacks == 1


def test_daily_pattern_route_returns_accepted_duplicate_and_requires_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(task_routes, "_session", lambda: session)
    outcomes = iter(("accepted", "duplicate"))

    def accept_request(_session, request):
        return AsyncEventAccepted(
            request_id=request.request_id,
            status=next(outcomes),
        )

    monkeypatch.setattr(
        task_routes,
        "_enqueue_daily_pattern_analysis",
        accept_request,
    )
    body = _request_body()
    client = TestClient(agent_main.app)

    accepted = client.post(
        task_routes.DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
        headers=AGENT_HEADERS,
        json=body,
    )
    duplicate = client.post(
        task_routes.DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
        headers=AGENT_HEADERS,
        json=body,
    )
    unauthorized = client.post(
        task_routes.DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
        json=body,
    )

    assert accepted.status_code == 202
    assert accepted.json() == {
        "request_id": body["request_id"],
        "status": "accepted",
    }
    assert duplicate.status_code == 200
    assert duplicate.json() == {
        "request_id": body["request_id"],
        "status": "duplicate",
    }
    assert unauthorized.status_code == 401
    assert unauthorized.json()["error"]["code"] == "UNAUTHORIZED"


def test_daily_pattern_route_maps_validation_to_public_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeSession()
    monkeypatch.setattr(task_routes, "_session", lambda: session)
    body = _request_body()
    body["patient_id"] = []

    response = TestClient(agent_main.app).post(
        task_routes.DAILY_MEDICATION_PATTERN_ANALYSIS_PATH,
        headers=AGENT_HEADERS,
        json=body,
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
