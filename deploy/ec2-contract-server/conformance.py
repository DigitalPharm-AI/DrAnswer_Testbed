from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if (PROJECT_ROOT / "contract_test_server").is_dir():
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx
from sqlalchemy import create_engine, text

from contract_test_server.models import ChatFeedbackRequest
from contract_test_server.storage import canonical_request_hash
from shared.async_v13_contracts import (
    MissedDoseResultCallback,
    NotificationPolicyChangeProposalRequest,
)
from shared.chat_contracts import ChatStreamEvent

ENV_FILE = Path("/etc/dranswer-agent-contract/contract.env")
RELEASE_ENV_FILE = Path("/etc/dranswer-agent-contract/release.env")
PUBLIC_PATHS = {
    "/agent/sync/chat",
    "/agent/async/chat_feedback",
    "/agent/async/missed-dose-events",
    "/agent/async/daily-medication-pattern-analysis",
}


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def public_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def contract_database_url(env: dict[str, str]) -> str:
    database_url = env.get("CONTRACT_DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("contract_database_configuration_missing")
    if not database_url.startswith(
        ("postgresql://", "postgresql+psycopg://")
    ):
        raise RuntimeError("contract_database_postgresql_required")
    return database_url


def request_record_count(
    env: dict[str, str],
    request_id: str,
) -> int:
    engine = create_engine(contract_database_url(env))
    try:
        with engine.connect() as connection:
            return int(
                connection.execute(
                    text(
                        """
                        SELECT COUNT(*)
                        FROM request_records
                        WHERE request_id = :request_id
                        """
                    ),
                    {"request_id": request_id},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def stored_request_hash(
    env: dict[str, str],
    *,
    api_path: str,
    request_id: str,
) -> str:
    engine = create_engine(contract_database_url(env))
    try:
        with engine.connect() as connection:
            value = connection.execute(
                text(
                    """
                    SELECT request_hash
                    FROM request_records
                    WHERE api_path = :api_path
                      AND request_id = :request_id
                    """
                ),
                {
                    "api_path": api_path,
                    "request_id": request_id,
                },
            ).scalar_one()
            return str(value)
    finally:
        engine.dispose()


def feedback_plaintext_present(
    env: dict[str, str],
    *,
    request_id: str,
    marker: str,
) -> bool:
    engine = create_engine(contract_database_url(env))
    try:
        with engine.connect() as connection:
            count = connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM request_records
                    WHERE request_id = :request_id
                      AND (
                          request_hash LIKE :marker
                          OR response_json LIKE :marker
                      )
                    """
                ),
                {
                    "request_id": request_id,
                    "marker": f"%{marker}%",
                },
            ).scalar_one()
            return int(count) > 0
    finally:
        engine.dispose()


def aware_time(minute: int) -> str:
    return f"2026-07-30T09:{minute:02d}:00+09:00"


def require_status(response: httpx.Response, expected: int) -> None:
    if response.status_code != expected:
        raise AssertionError(
            f"status_mismatch:path={response.request.url.path}:"
            f"actual={response.status_code}:expected={expected}"
        )


def require_error(
    response: httpx.Response,
    *,
    status_code: int,
    code: str,
    request_id: str | None,
) -> dict[str, Any]:
    require_status(response, status_code)
    body = response.json()
    if set(body) != {"request_id", "error"}:
        raise AssertionError("error_top_level_shape_mismatch")
    if body["request_id"] != request_id:
        raise AssertionError("error_request_id_mismatch")
    error = body["error"]
    if set(error) != {"code", "message", "retryable", "details"}:
        raise AssertionError("error_object_shape_mismatch")
    if error["code"] != code:
        raise AssertionError(
            f"error_code_mismatch:{error['code']}:{code}"
        )
    if not isinstance(error["message"], str) or not error["message"]:
        raise AssertionError("error_message_missing")
    if not isinstance(error["retryable"], bool):
        raise AssertionError("error_retryable_not_boolean")
    if error["details"] is not None and not isinstance(
        error["details"],
        dict,
    ):
        raise AssertionError("error_details_shape_mismatch")
    return body


def ndjson_events(response: httpx.Response) -> list[ChatStreamEvent]:
    require_status(response, 200)
    if not response.headers["content-type"].startswith(
        "application/x-ndjson"
    ):
        raise AssertionError("ndjson_content_type_missing")
    if response.headers.get("cache-control") != "no-cache":
        raise AssertionError("ndjson_cache_control_mismatch")
    if response.headers.get("x-accel-buffering") != "no":
        raise AssertionError("ndjson_proxy_buffering_header_mismatch")
    if not response.content.endswith(b"\n"):
        raise AssertionError("ndjson_missing_final_lf")
    if b"\n\n" in response.content or b"\r\n" in response.content:
        raise AssertionError("ndjson_framing_invalid")
    rows = [
        json.loads(line)
        for line in response.content.splitlines()
        if line
    ]
    events = [ChatStreamEvent.model_validate(row) for row in rows]
    if [event.sequence for event in events] != list(range(len(events))):
        raise AssertionError("ndjson_sequence_not_contiguous")
    terminals = [
        event
        for event in events
        if event.status in {"completed", "error"}
    ]
    if len(terminals) != 1 or terminals[0] is not events[-1]:
        raise AssertionError("ndjson_terminal_invariant_failed")
    serialized = response.text.lower()
    for forbidden in (
        "trace_id",
        "chain_of_thought",
        "private_reasoning",
        "job_id",
    ):
        if forbidden in serialized:
            raise AssertionError(f"private_field_exposed:{forbidden}")
    return events


class Suite:
    def __init__(self, *, phase: str, release: str) -> None:
        self.phase = phase
        self.release = release
        self.started_at = datetime.now(UTC)
        self.results: list[dict[str, Any]] = []

    def run(
        self,
        case_id: str,
        area: str,
        expected: str,
        function: Callable[[], dict[str, Any] | None],
    ) -> None:
        started = time.perf_counter()
        try:
            evidence = function() or {}
            passed = True
            error = None
        except Exception as exc:  # test runner must preserve all case results
            evidence = {}
            passed = False
            error = f"{type(exc).__name__}:{str(exc)[:240]}"
        self.results.append(
            {
                "case_id": case_id,
                "area": area,
                "expected": expected,
                "passed": passed,
                "duration_ms": round(
                    (time.perf_counter() - started) * 1000,
                    2,
                ),
                "evidence": evidence,
                "error": error,
            }
        )

    def document(self) -> dict[str, Any]:
        passed = sum(result["passed"] for result in self.results)
        return {
            "schema_version": "1",
            "suite": "DRAnswer AI v1.3 live HTTP conformance",
            "phase": self.phase,
            "release": self.release,
            "started_at": self.started_at.isoformat(),
            "completed_at": datetime.now(UTC).isoformat(),
            "summary": {
                "total": len(self.results),
                "passed": passed,
                "failed": len(self.results) - passed,
            },
            "results": self.results,
            "not_exercised": [
                "404 ownership/not-found branches require a real Backend read view",
                "500/503/504 fault injection was not performed on the shared test endpoint",
                "Outbound callback delivery requires the future Backend URL and token",
                "Medical/LLM answer quality is outside this deterministic contract stub",
            ],
        }


def callback_rows(
    client: httpx.Client,
    control_headers: dict[str, str],
) -> list[dict[str, Any]]:
    response = client.get(
        "/_test/callbacks?limit=500",
        headers=control_headers,
    )
    require_status(response, 200)
    return response.json()["callbacks"]


def run_initial(
    *,
    client: httpx.Client,
    env: dict[str, str],
    state_path: Path,
) -> dict[str, Any]:
    suite = Suite(phase="initial", release=env["APP_RELEASE_VERSION"])
    agent_headers = {
        "Authorization": f"Bearer {env['AGENT_SYNC_API_TOKEN']}",
    }
    control_headers = {
        "X-Test-Control-Token": env["TEST_CONTROL_TOKEN"],
    }
    ndjson_headers = {
        **agent_headers,
        "Accept": "application/x-ndjson",
    }
    state: dict[str, Any] = {
        "release": env["APP_RELEASE_VERSION"],
    }
    unauthorized_ids: list[str] = []

    def health_case() -> dict[str, Any]:
        response = client.get("/health")
        require_status(response, 200)
        body = response.json()
        assert body["status"] == "ok"
        assert body["release"] == env["APP_RELEASE_VERSION"]
        return {"status": body["status"], "release": body["release"]}

    suite.run(
        "OPS-001",
        "operations",
        "GET /health returns deployed release",
        health_case,
    )

    def ready_case() -> dict[str, Any]:
        response = client.get("/health/ready")
        require_status(response, 200)
        body = response.json()
        assert body["status"] == "ready"
        assert body["release"] == env["APP_RELEASE_VERSION"]
        assert body["database_ready"] is True
        assert body["callback_mode"] == "hold"
        assert body["callback_delivery_ready"] is False
        assert body["errors"] == []
        return {
            "status": body["status"],
            "database_ready": body["database_ready"],
            "callback_mode": body["callback_mode"],
            "callback_delivery_ready": body[
                "callback_delivery_ready"
            ],
        }

    suite.run(
        "OPS-002",
        "operations",
        "ready in intentional callback hold mode",
        ready_case,
    )

    def artifact_case() -> dict[str, Any]:
        mapping = {
            "chat.json": "AI_V13_CHAT_OPENAPI.json",
            "async-medication.json": (
                "AI_V13_ASYNC_MEDICATION_OPENAPI.json"
            ),
            "backend-callbacks.json": (
                "BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json"
            ),
        }
        hashes: dict[str, str] = {}
        for route_name, file_name in mapping.items():
            response = client.get(f"/contracts/v1.3/{route_name}")
            require_status(response, 200)
            expected = (
                Path(env["CONTRACT_SPECS_DIR"]) / file_name
            ).read_bytes()
            assert response.content == expected
            hashes[route_name] = hashlib.sha256(
                response.content
            ).hexdigest()
        return {"sha256": hashes}

    suite.run(
        "SPEC-001",
        "contract artifacts",
        "served v1.3 artifacts are byte-identical to deployed files",
        artifact_case,
    )

    def openapi_case() -> dict[str, Any]:
        response = client.get("/openapi.json")
        require_status(response, 200)
        schema = response.json()
        assert set(schema["paths"]) == PUBLIC_PATHS
        for path in PUBLIC_PATHS:
            operation = schema["paths"][path]["post"]
            assert operation["security"] == [{"AgentSyncBearer": []}]
            assert "422" not in operation["responses"]
        chat_content = schema["paths"]["/agent/sync/chat"]["post"][
            "responses"
        ]["200"]["content"]
        assert set(chat_content) == {"application/x-ndjson"}
        return {
            "public_paths": sorted(schema["paths"]),
            "chat_media_type": next(iter(chat_content)),
        }

    suite.run(
        "SPEC-002",
        "OpenAPI",
        "only four public routes, Bearer security, no 422 drift",
        openapi_case,
    )

    auth_requests = [
        (
            "AUTH-001",
            "/agent/sync/chat",
            {
                "request_id": public_id("req"),
                "message_id": public_id("user_msg"),
                "patient_id": public_id("patient"),
                "requested_return_type": "text",
                "message": "auth test",
                "message_at": aware_time(1),
            },
            {"Accept": "application/x-ndjson"},
        ),
        (
            "AUTH-002",
            "/agent/async/chat_feedback",
            {
                "request_id": public_id("req"),
                "message_id": public_id("assistant_msg"),
                "patient_id": public_id("patient"),
                "reaction": "like",
                "feedback_at": aware_time(2),
            },
            {"Authorization": "Bearer definitely-wrong"},
        ),
        (
            "AUTH-003",
            "/agent/async/missed-dose-events",
            {
                "request_id": public_id("req"),
                "patient_id": public_id("patient"),
                "dose_event_id": public_id("dose"),
            },
            {"Authorization": "Basic dGVzdDp0ZXN0"},
        ),
        (
            "AUTH-004",
            "/agent/async/daily-medication-pattern-analysis",
            {
                "request_id": public_id("req"),
                "patient_id": [public_id("patient")],
                "analysis_date": "2026-07-30",
            },
            {},
        ),
    ]
    for case_id, path, payload, headers in auth_requests:
        unauthorized_ids.append(str(payload["request_id"]))

        def auth_case(
            path: str = path,
            payload: dict[str, Any] = payload,
            headers: dict[str, str] = headers,
        ) -> dict[str, Any]:
            response = client.post(path, json=payload, headers=headers)
            body = require_error(
                response,
                status_code=401,
                code="UNAUTHORIZED",
                request_id=str(payload["request_id"]),
            )
            return {
                "status": response.status_code,
                "code": body["error"]["code"],
            }

        suite.run(
            case_id,
            "authentication",
            f"{path} rejects missing/wrong/non-Bearer credentials",
            auth_case,
        )

    def test_control_auth_case() -> dict[str, Any]:
        response = client.get("/_test/callbacks")
        require_status(response, 401)
        assert response.json() == {"detail": "unauthorized"}
        return {"status": response.status_code}

    suite.run(
        "AUTH-005",
        "authentication",
        "internal callback inspection requires separate control token",
        test_control_auth_case,
    )

    def auth_no_persistence_case() -> dict[str, Any]:
        count = sum(
            request_record_count(env, request_id)
            for request_id in unauthorized_ids
        )
        assert count == 0
        return {"persisted_unauthorized_requests": count}

    suite.run(
        "AUTH-006",
        "authentication",
        "rejected calls do not mutate request storage",
        auth_no_persistence_case,
    )

    chat_patient = public_id("patient")
    chat_payloads: dict[str, dict[str, Any]] = {}

    def execute_chat_type(
        return_type: str,
        *,
        accept: str,
    ) -> dict[str, Any]:
        payload = {
            "request_id": public_id("req"),
            "message_id": public_id("user_msg"),
            "patient_id": chat_patient,
            "requested_return_type": return_type,
            "message": (
                '{"temperature":36.5}'
                if return_type == "input_box"
                else f"synthetic {return_type} request"
            ),
            "message_at": aware_time(3),
        }
        chat_payloads[return_type] = payload
        response = client.post(
            "/agent/sync/chat",
            json=payload,
            headers={**agent_headers, "Accept": accept},
        )
        events = ndjson_events(response)
        assert len(events) == 2
        assert [event.status for event in events] == [
            "streaming",
            "completed",
        ]
        assert all(
            event.request_id == payload["request_id"]
            and event.message_id == payload["message_id"]
            for event in events
        )
        assert events[-1].message_type == return_type
        if return_type == "selection_box":
            assert events[-1].message is not None
            assert events[-1].message.selections
            assert events[-1].message.inputs is None
        if return_type == "input_box":
            assert events[-1].message is not None
            assert events[-1].message.inputs
            assert events[-1].message.selections is None
        if return_type == "text":
            state["chat_payload"] = payload
            state["chat_terminal"] = events[-1].model_dump(mode="json")
        return {
            "status": response.status_code,
            "events": len(events),
            "sequences": [event.sequence for event in events],
            "terminal_type": events[-1].message_type,
        }

    for case_id, return_type, accept in (
        (
            "CHAT-001",
            "text",
            "text/plain, Application/X-NDJSON; charset=utf-8",
        ),
        ("CHAT-002", "selection_box", "application/x-ndjson"),
        ("CHAT-003", "input_box", "application/x-ndjson"),
    ):
        suite.run(
            case_id,
            "sync chat",
            f"{return_type} returns valid LF-framed NDJSON",
            lambda return_type=return_type, accept=accept: execute_chat_type(
                return_type,
                accept=accept,
            ),
        )

    def chat_replay_case() -> dict[str, Any]:
        payload = chat_payloads["text"]
        response = client.post(
            "/agent/sync/chat",
            json=payload,
            headers=ndjson_headers,
        )
        events = ndjson_events(response)
        assert len(events) == 1
        assert events[0].sequence == 0
        assert events[0].status == "completed"
        assert (
            events[0].event_at.isoformat()
            == ChatStreamEvent.model_validate(
                state["chat_terminal"]
            ).event_at.isoformat()
        )
        return {
            "status": response.status_code,
            "events": len(events),
            "terminal_sequence": events[0].sequence,
        }

    suite.run(
        "CHAT-004",
        "sync chat idempotency",
        "same body replays one stored terminal event at sequence 0",
        chat_replay_case,
    )

    def chat_conflict_case() -> dict[str, Any]:
        payload = {
            **chat_payloads["text"],
            "message": "different synthetic message",
        }
        response = client.post(
            "/agent/sync/chat",
            json=payload,
            headers=ndjson_headers,
        )
        body = require_error(
            response,
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            request_id=str(payload["request_id"]),
        )
        return {
            "status": response.status_code,
            "code": body["error"]["code"],
        }

    suite.run(
        "CHAT-005",
        "sync chat idempotency",
        "changed body with same request_id returns 409",
        chat_conflict_case,
    )

    chat_invalid_cases = [
        (
            "CHAT-006",
            {**chat_payloads["selection_box"]},
            {**agent_headers},
            "missing Accept",
        ),
        (
            "CHAT-007",
            {
                **chat_payloads["selection_box"],
                "request_id": public_id("req"),
                "conversation_id": "retired",
            },
            ndjson_headers,
            "extra retired field",
        ),
        (
            "CHAT-008",
            {
                **chat_payloads["selection_box"],
                "request_id": public_id("req"),
                "message_at": "2026-07-30T09:00:00",
            },
            ndjson_headers,
            "naive datetime",
        ),
        (
            "CHAT-009",
            {
                **chat_payloads["input_box"],
                "request_id": public_id("req"),
                "message": "not-json",
            },
            ndjson_headers,
            "invalid input_box JSON string",
        ),
    ]
    for case_id, payload, headers, description in chat_invalid_cases:

        def invalid_chat_case(
            payload: dict[str, Any] = payload,
            headers: dict[str, str] = headers,
        ) -> dict[str, Any]:
            response = client.post(
                "/agent/sync/chat",
                json=payload,
                headers=headers,
            )
            body = require_error(
                response,
                status_code=400,
                code="INVALID_REQUEST",
                request_id=str(payload["request_id"]),
            )
            return {
                "status": response.status_code,
                "code": body["error"]["code"],
            }

        suite.run(
            case_id,
            "sync chat validation",
            f"{description} returns contract 400 instead of 422",
            invalid_chat_case,
        )

    def invalid_id_case() -> dict[str, Any]:
        payload = {
            **chat_payloads["selection_box"],
            "request_id": "req_NOT_LOWER_HEX",
        }
        response = client.post(
            "/agent/sync/chat",
            json=payload,
            headers=ndjson_headers,
        )
        body = require_error(
            response,
            status_code=400,
            code="INVALID_REQUEST",
            request_id=None,
        )
        return {
            "status": response.status_code,
            "request_id": body["request_id"],
        }

    suite.run(
        "CHAT-010",
        "sync chat validation",
        "invalid public request ID returns request_id null",
        invalid_id_case,
    )

    def malformed_json_case() -> dict[str, Any]:
        response = client.post(
            "/agent/sync/chat",
            content=b'{"request_id":',
            headers={
                **ndjson_headers,
                "Content-Type": "application/json",
            },
        )
        body = require_error(
            response,
            status_code=400,
            code="INVALID_REQUEST",
            request_id=None,
        )
        return {
            "status": response.status_code,
            "request_id": body["request_id"],
        }

    suite.run(
        "CHAT-011",
        "sync chat validation",
        "malformed JSON returns safe contract 400",
        malformed_json_case,
    )

    feedback_patient = public_id("patient")

    def feedback_variant(
        *,
        reaction: str | None,
        feedback_text: str | None,
        minute: int,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "request_id": public_id("req"),
            "message_id": public_id("assistant_msg"),
            "patient_id": feedback_patient,
            "reaction": reaction,
            "feedback_text": feedback_text,
            "feedback_at": aware_time(minute),
        }
        response = client.post(
            "/agent/async/chat_feedback",
            json=payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        body = response.json()
        assert set(body) == {"status", "reaction", "accepted_at"}
        assert body["status"] == "accepted"
        assert body["reaction"] == reaction
        assert datetime.fromisoformat(body["accepted_at"]).utcoffset() is not None
        return {
            "status": response.status_code,
            "reaction": body["reaction"],
            "accepted_at_aware": True,
        }

    suite.run(
        "FB-001",
        "chat feedback",
        "reaction-only feedback is accepted",
        lambda: feedback_variant(
            reaction="like",
            feedback_text=None,
            minute=10,
        ),
    )
    suite.run(
        "FB-002",
        "chat feedback",
        "text-only feedback is accepted",
        lambda: feedback_variant(
            reaction=None,
            feedback_text="synthetic text-only feedback",
            minute=11,
        ),
    )

    feedback_marker = f"synthetic-conformance-{secrets.token_hex(8)}"
    feedback_payload = {
        "request_id": public_id("req"),
        "message_id": public_id("assistant_msg"),
        "patient_id": feedback_patient,
        "reaction": "like",
        "feedback_text": f"  {feedback_marker}  ",
        "feedback_at": aware_time(12),
    }

    def feedback_both_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/chat_feedback",
            json=feedback_payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        state["feedback_payload"] = feedback_payload
        state["feedback_response"] = response.json()
        return {
            "status": response.status_code,
            "reaction": response.json()["reaction"],
        }

    suite.run(
        "FB-003",
        "chat feedback",
        "reaction plus text is accepted",
        feedback_both_case,
    )

    def feedback_replay_case() -> dict[str, Any]:
        replay_payload = {
            **feedback_payload,
            "feedback_text": feedback_marker,
        }
        response = client.post(
            "/agent/async/chat_feedback",
            json=replay_payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        assert response.json() == state["feedback_response"]
        return {
            "status": response.status_code,
            "exact_response_replay": True,
        }

    suite.run(
        "FB-004",
        "chat feedback idempotency",
        "trim-normalized same body replays exact 202 response",
        feedback_replay_case,
    )

    def feedback_conflict_case() -> dict[str, Any]:
        payload = {
            **feedback_payload,
            "reaction": "dislike",
        }
        response = client.post(
            "/agent/async/chat_feedback",
            json=payload,
            headers=agent_headers,
        )
        body = require_error(
            response,
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            request_id=str(payload["request_id"]),
        )
        return {
            "status": response.status_code,
            "code": body["error"]["code"],
        }

    suite.run(
        "FB-005",
        "chat feedback idempotency",
        "changed normalized body returns 409",
        feedback_conflict_case,
    )

    feedback_invalid_cases: list[
        tuple[str, dict[str, Any], str]
    ] = [
        (
            "FB-006",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "reaction": None,
                "feedback_text": None,
            },
            "reaction and text both null",
        ),
        (
            "FB-007",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "feedback_text": "   ",
            },
            "blank text",
        ),
        (
            "FB-008",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "feedback_text": "x" * 4_001,
            },
            "text over 4000 characters",
        ),
        (
            "FB-009",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "feedback_at": "2026-07-30T09:12:00",
            },
            "naive feedback time",
        ),
        (
            "FB-010",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "message_id": public_id("user_msg"),
            },
            "user message ID in assistant field",
        ),
        (
            "FB-011",
            {
                **feedback_payload,
                "request_id": public_id("req"),
                "conversation_id": "retired",
            },
            "extra retired field",
        ),
    ]
    for case_id, payload, description in feedback_invalid_cases:

        def invalid_feedback_case(
            payload: dict[str, Any] = payload,
        ) -> dict[str, Any]:
            response = client.post(
                "/agent/async/chat_feedback",
                json=payload,
                headers=agent_headers,
            )
            body = require_error(
                response,
                status_code=400,
                code="INVALID_REQUEST",
                request_id=str(payload["request_id"]),
            )
            return {
                "status": response.status_code,
                "code": body["error"]["code"],
            }

        suite.run(
            case_id,
            "chat feedback validation",
            f"{description} returns contract 400",
            invalid_feedback_case,
        )

    def feedback_privacy_case() -> dict[str, Any]:
        normalized = ChatFeedbackRequest.model_validate(
            feedback_payload
        ).model_dump(mode="json")
        plain_digest = canonical_request_hash(normalized)
        stored_digest = stored_request_hash(
            env,
            api_path="/agent/async/chat_feedback",
            request_id=str(feedback_payload["request_id"]),
        )
        plaintext_present = feedback_plaintext_present(
            env,
            request_id=str(feedback_payload["request_id"]),
            marker=feedback_marker,
        )
        assert plaintext_present is False
        assert stored_digest != plain_digest
        return {
            "plaintext_present": plaintext_present,
            "plain_sha256_stored": stored_digest == plain_digest,
            "digest_length": len(stored_digest),
            "database_kind": "postgresql",
        }

    suite.run(
        "FB-012",
        "chat feedback privacy",
        "free text is absent and request digest is keyed, not plain SHA",
        feedback_privacy_case,
    )

    def endpoint_scope_case() -> dict[str, Any]:
        shared_request_id = str(chat_payloads["text"]["request_id"])
        payload = {
            "request_id": shared_request_id,
            "message_id": public_id("assistant_msg"),
            "patient_id": feedback_patient,
            "reaction": "dislike",
            "feedback_at": aware_time(13),
        }
        response = client.post(
            "/agent/async/chat_feedback",
            json=payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        count = request_record_count(env, shared_request_id)
        assert count == 2
        return {
            "status": response.status_code,
            "same_request_id_records": count,
        }

    suite.run(
        "IDEM-001",
        "idempotency scope",
        "same request_id is independent across endpoint paths",
        endpoint_scope_case,
    )

    missed_payload = {
        "request_id": public_id("req"),
        "patient_id": public_id("patient"),
        "dose_event_id": public_id("dose"),
    }

    def missed_accept_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/missed-dose-events",
            json=missed_payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        assert response.json() == {
            "request_id": missed_payload["request_id"],
            "status": "accepted",
        }
        state["missed_payload"] = missed_payload
        return response.json()

    suite.run(
        "MISS-001",
        "missed-dose acceptance",
        "new event returns 202 accepted",
        missed_accept_case,
    )

    def missed_duplicate_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/missed-dose-events",
            json=missed_payload,
            headers=agent_headers,
        )
        require_status(response, 200)
        assert response.json() == {
            "request_id": missed_payload["request_id"],
            "status": "duplicate",
        }
        return response.json()

    suite.run(
        "MISS-002",
        "missed-dose idempotency",
        "same event returns 200 duplicate",
        missed_duplicate_case,
    )

    def missed_conflict_case() -> dict[str, Any]:
        payload = {
            **missed_payload,
            "dose_event_id": public_id("dose"),
        }
        response = client.post(
            "/agent/async/missed-dose-events",
            json=payload,
            headers=agent_headers,
        )
        body = require_error(
            response,
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            request_id=str(payload["request_id"]),
        )
        return {
            "status": response.status_code,
            "code": body["error"]["code"],
        }

    suite.run(
        "MISS-003",
        "missed-dose idempotency",
        "changed event body returns 409",
        missed_conflict_case,
    )

    for case_id, payload, description in (
        (
            "MISS-004",
            {
                **missed_payload,
                "request_id": public_id("req"),
                "dose_event_id": "dose_INVALID",
            },
            "invalid dose ID",
        ),
        (
            "MISS-005",
            {
                **missed_payload,
                "request_id": public_id("req"),
                "extra": True,
            },
            "extra field",
        ),
    ):

        def invalid_missed_case(
            payload: dict[str, Any] = payload,
        ) -> dict[str, Any]:
            response = client.post(
                "/agent/async/missed-dose-events",
                json=payload,
                headers=agent_headers,
            )
            body = require_error(
                response,
                status_code=400,
                code="INVALID_REQUEST",
                request_id=str(payload["request_id"]),
            )
            return {
                "status": response.status_code,
                "code": body["error"]["code"],
            }

        suite.run(
            case_id,
            "missed-dose validation",
            f"{description} returns contract 400",
            invalid_missed_case,
        )

    daily_patients = [public_id("patient"), public_id("patient")]
    daily_payload = {
        "request_id": public_id("req"),
        "patient_id": daily_patients,
        "analysis_date": "2026-07-30",
    }

    def daily_accept_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=daily_payload,
            headers=agent_headers,
        )
        require_status(response, 202)
        assert response.json() == {
            "request_id": daily_payload["request_id"],
            "status": "accepted",
        }
        state["daily_payload"] = daily_payload
        return response.json()

    suite.run(
        "DAILY-001",
        "daily analysis acceptance",
        "new two-patient request returns 202 accepted",
        daily_accept_case,
    )

    def daily_duplicate_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=daily_payload,
            headers=agent_headers,
        )
        require_status(response, 200)
        assert response.json()["status"] == "duplicate"
        return response.json()

    suite.run(
        "DAILY-002",
        "daily analysis idempotency",
        "same body returns 200 duplicate",
        daily_duplicate_case,
    )

    def daily_conflict_case() -> dict[str, Any]:
        payload = {
            **daily_payload,
            "patient_id": list(reversed(daily_patients)),
        }
        response = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=payload,
            headers=agent_headers,
        )
        body = require_error(
            response,
            status_code=409,
            code="IDEMPOTENCY_CONFLICT",
            request_id=str(payload["request_id"]),
        )
        return {
            "status": response.status_code,
            "code": body["error"]["code"],
        }

    suite.run(
        "DAILY-003",
        "daily analysis idempotency",
        "patient array order change returns 409",
        daily_conflict_case,
    )

    daily_invalid_cases = [
        (
            "DAILY-004",
            {**daily_payload, "request_id": public_id("req"), "patient_id": []},
            "empty patient array",
        ),
        (
            "DAILY-005",
            {
                **daily_payload,
                "request_id": public_id("req"),
                "patient_id": [daily_patients[0], daily_patients[0]],
            },
            "duplicate patients",
        ),
        (
            "DAILY-006",
            {
                **daily_payload,
                "request_id": public_id("req"),
                "patient_id": daily_patients[0],
            },
            "scalar patient ID",
        ),
        (
            "DAILY-007",
            {
                **daily_payload,
                "request_id": public_id("req"),
                "analysis_date": "2026-02-30",
            },
            "invalid date",
        ),
        (
            "DAILY-008",
            {
                **daily_payload,
                "request_id": public_id("req"),
                "conversation_id": "retired",
            },
            "extra retired field",
        ),
    ]
    for case_id, payload, description in daily_invalid_cases:

        def invalid_daily_case(
            payload: dict[str, Any] = payload,
        ) -> dict[str, Any]:
            response = client.post(
                "/agent/async/daily-medication-pattern-analysis",
                json=payload,
                headers=agent_headers,
            )
            body = require_error(
                response,
                status_code=400,
                code="INVALID_REQUEST",
                request_id=str(payload["request_id"]),
            )
            return {
                "status": response.status_code,
                "code": body["error"]["code"],
            }

        suite.run(
            case_id,
            "daily analysis validation",
            f"{description} returns contract 400",
            invalid_daily_case,
        )

    def callback_hold_case() -> dict[str, Any]:
        rows = callback_rows(client, control_headers)
        selected = [
            row
            for row in rows
            if row["source_request_id"]
            in {
                missed_payload["request_id"],
                daily_payload["request_id"],
            }
        ]
        assert len(selected) == 3
        assert all(row["status"] == "held" for row in selected)
        assert all(row["attempts"] == 0 for row in selected)
        for row in selected:
            if row["callback_kind"] == "missed_dose_result":
                MissedDoseResultCallback.model_validate(row["payload"])
            else:
                NotificationPolicyChangeProposalRequest.model_validate(
                    row["payload"]
                )
        state["callback_request_ids"] = [
            row["callback_request_id"] for row in selected
        ]
        return {
            "callbacks": len(selected),
            "statuses": sorted({row["status"] for row in selected}),
            "attempts": sorted({row["attempts"] for row in selected}),
            "paths": sorted({row["callback_path"] for row in selected}),
        }

    suite.run(
        "CALLBACK-001",
        "callback outbox",
        "three spec-valid callbacks are held with attempts 0",
        callback_hold_case,
    )

    def callback_release_refused_case() -> dict[str, Any]:
        callback_request_id = state["callback_request_ids"][0]
        response = client.post(
            f"/_test/callbacks/{callback_request_id}/release",
            headers=control_headers,
        )
        require_status(response, 409)
        assert response.json() == {
            "detail": "callback_delivery_not_configured"
        }
        selected = {
            row["callback_request_id"]: row
            for row in callback_rows(client, control_headers)
        }
        assert selected[callback_request_id]["status"] == "held"
        assert selected[callback_request_id]["attempts"] == 0
        return {
            "status": response.status_code,
            "callback_status_after": "held",
            "attempts_after": 0,
        }

    suite.run(
        "CALLBACK-002",
        "callback containment",
        "release is refused while Backend delivery is unconfigured",
        callback_release_refused_case,
    )

    def callback_no_attempt_case() -> dict[str, Any]:
        time.sleep(0.2)
        selected = {
            row["callback_request_id"]: row
            for row in callback_rows(client, control_headers)
        }
        callbacks = [
            selected[request_id]
            for request_id in state["callback_request_ids"]
        ]
        assert all(row["status"] == "held" for row in callbacks)
        assert all(row["attempts"] == 0 for row in callbacks)
        return {
            "held": len(callbacks),
            "max_attempts": max(row["attempts"] for row in callbacks),
        }

    suite.run(
        "CALLBACK-003",
        "callback containment",
        "hold mode performs no delivery attempts",
        callback_no_attempt_case,
    )

    state_path.write_text(
        json.dumps(
            state,
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return suite.document()


def run_replay(
    *,
    client: httpx.Client,
    env: dict[str, str],
    state_path: Path,
) -> dict[str, Any]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    suite = Suite(phase="post_restart", release=env["APP_RELEASE_VERSION"])
    agent_headers = {
        "Authorization": f"Bearer {env['AGENT_SYNC_API_TOKEN']}",
    }
    control_headers = {
        "X-Test-Control-Token": env["TEST_CONTROL_TOKEN"],
    }

    def release_case() -> dict[str, Any]:
        health = client.get("/health")
        ready = client.get("/health/ready")
        require_status(health, 200)
        require_status(ready, 200)
        assert health.json()["release"] == state["release"]
        assert ready.json()["status"] == "ready"
        return {
            "release": health.json()["release"],
            "ready": ready.json()["status"],
        }

    suite.run(
        "RESTART-001",
        "restart persistence",
        "same release becomes ready after systemd restart",
        release_case,
    )

    def chat_replay_case() -> dict[str, Any]:
        response = client.post(
            "/agent/sync/chat",
            json=state["chat_payload"],
            headers={
                **agent_headers,
                "Accept": "application/x-ndjson",
            },
        )
        events = ndjson_events(response)
        assert len(events) == 1
        expected = {
            **state["chat_terminal"],
            "sequence": 0,
        }
        assert events[0].model_dump(mode="json") == expected
        return {
            "events": len(events),
            "sequence": events[0].sequence,
            "stored_terminal_exact": True,
        }

    suite.run(
        "RESTART-002",
        "restart persistence",
        "chat terminal replay survives PostgreSQL reconnect",
        chat_replay_case,
    )

    def feedback_replay_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/chat_feedback",
            json=state["feedback_payload"],
            headers=agent_headers,
        )
        require_status(response, 202)
        assert response.json() == state["feedback_response"]
        return {
            "status": response.status_code,
            "exact_response_replay": True,
        }

    suite.run(
        "RESTART-003",
        "restart persistence",
        "feedback accepted_at and reaction replay exactly",
        feedback_replay_case,
    )

    def missed_replay_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/missed-dose-events",
            json=state["missed_payload"],
            headers=agent_headers,
        )
        require_status(response, 200)
        assert response.json()["status"] == "duplicate"
        return response.json()

    suite.run(
        "RESTART-004",
        "restart persistence",
        "missed-dose idempotency survives restart",
        missed_replay_case,
    )

    def daily_replay_case() -> dict[str, Any]:
        response = client.post(
            "/agent/async/daily-medication-pattern-analysis",
            json=state["daily_payload"],
            headers=agent_headers,
        )
        require_status(response, 200)
        assert response.json()["status"] == "duplicate"
        return response.json()

    suite.run(
        "RESTART-005",
        "restart persistence",
        "daily-analysis idempotency survives restart",
        daily_replay_case,
    )

    def callbacks_case() -> dict[str, Any]:
        selected = {
            row["callback_request_id"]: row
            for row in callback_rows(client, control_headers)
        }
        callbacks = [
            selected[request_id]
            for request_id in state["callback_request_ids"]
        ]
        assert len(callbacks) == 3
        assert all(row["status"] == "held" for row in callbacks)
        assert all(row["attempts"] == 0 for row in callbacks)
        return {
            "callbacks": len(callbacks),
            "status": "held",
            "attempts": 0,
        }

    suite.run(
        "RESTART-006",
        "restart persistence",
        "held callbacks remain held with attempts 0",
        callbacks_case,
    )
    return suite.document()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live v1.3 HTTP conformance checks without printing secrets.",
    )
    parser.add_argument(
        "--phase",
        choices=("initial", "post-restart"),
        required=True,
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=Path(
            "/home/ec2-user/contract-conformance-state.json"
        ),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    env = {
        **read_env(ENV_FILE),
        **read_env(RELEASE_ENV_FILE),
    }
    base_url = f"http://127.0.0.1:{env.get('CONTRACT_PORT', '8701')}"
    with httpx.Client(
        base_url=base_url,
        timeout=10,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        if args.phase == "initial":
            document = run_initial(
                client=client,
                env=env,
                state_path=args.state,
            )
        else:
            document = run_replay(
                client=client,
                env=env,
                state_path=args.state,
            )

    args.output.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    summary = document["summary"]
    print(
        json.dumps(
            {
                "phase": document["phase"],
                "release": document["release"],
                **summary,
                "output": str(args.output),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
